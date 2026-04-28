from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_monty import FunctionSnapshot, FutureSnapshot, Monty, MontyComplete, NameLookupSnapshot


ALLOWED_ACTIVITY_NAMES = {"azure_rest", "echo"}
MAX_ACTIVITY_FAN_OUT = 64
MAX_PRINT_OUTPUT_CHARS = 8192

MONTY_PRELUDE = """
import asyncio

async def when_all(awaitables):
    return await asyncio.gather(*awaitables)
"""


@dataclass(frozen=True)
class ActivitySpec:
    name: str
    payload: Any


@dataclass(frozen=True)
class ActivityWork:
    spec: ActivitySpec
    task: Any


@dataclass(frozen=True)
class WhenAnyWork:
    specs: list[ActivitySpec]
    tasks: list[Any]


def run_monty_orchestration(context: Any, code: str):
    bridge = MontyDurableBridge(context)
    return (yield from bridge.run(code))


class MontyDurableBridge:
    def __init__(self, context: Any):
        self.context = context
        self.pending_work: dict[int, ActivityWork | WhenAnyWork] = {}
        self.print_chunks: list[str] = []
        self.print_truncated = False
        self.scheduled_activity_count = 0

    def run(self, code: str):
        if not isinstance(code, str) or not code.strip():
            raise ValueError("The orchestrator input must be a non-empty Python code string.")

        monty = Monty(_build_code(code), script_name="orchestrator.py")
        progress = monty.start(print_callback=self._collect_print)

        while True:
            if isinstance(progress, MontyComplete):
                return self._complete(progress)
            if isinstance(progress, FunctionSnapshot):
                progress = self._handle_function_snapshot(progress)
                continue
            if isinstance(progress, FutureSnapshot):
                progress = yield from self._handle_future_snapshot(progress)
                continue
            if isinstance(progress, NameLookupSnapshot):
                raise RuntimeError(f"Name lookup is not supported in orchestration code: {progress.variable_name!r}")
            raise RuntimeError(f"Unsupported Monty progress type: {type(progress).__name__}")

    def _handle_function_snapshot(self, snapshot: FunctionSnapshot) -> Any:
        if snapshot.is_os_function:
            return snapshot.resume(
                {
                    "exc_type": "PermissionError",
                    "message": "Filesystem and OS calls are not available in the Durable orchestrator.",
                }
            )

        function_name = str(snapshot.function_name)
        if function_name == "call_activity":
            return self._schedule_call_activity(snapshot)
        if function_name == "when_any":
            return self._schedule_when_any(snapshot)

        return snapshot.resume(
            {
                "exc_type": "NameError",
                "message": f"External function {function_name!r} is not available.",
            }
        )

    def _schedule_call_activity(self, snapshot: FunctionSnapshot) -> Any:
        try:
            spec = _parse_activity_call(snapshot.args, snapshot.kwargs)
            task = self.context.call_activity(spec.name, spec.payload)
            self.pending_work[int(snapshot.call_id)] = ActivityWork(spec=spec, task=task)
            self.scheduled_activity_count += 1
        except Exception as exc:
            return snapshot.resume(_external_error(exc))

        return snapshot.resume({"future": ...})

    def _schedule_when_any(self, snapshot: FunctionSnapshot) -> Any:
        try:
            specs = _parse_when_any_specs(snapshot.args, snapshot.kwargs)
            tasks = [self.context.call_activity(spec.name, spec.payload) for spec in specs]
            self.pending_work[int(snapshot.call_id)] = WhenAnyWork(specs=specs, tasks=tasks)
            self.scheduled_activity_count += len(tasks)
        except Exception as exc:
            return snapshot.resume(_external_error(exc))

        return snapshot.resume({"future": ...})

    def _handle_future_snapshot(self, snapshot: FutureSnapshot):
        pending_call_ids = [int(call_id) for call_id in snapshot.pending_call_ids]
        if not pending_call_ids:
            return snapshot.resume({})

        missing_call_ids = [call_id for call_id in pending_call_ids if call_id not in self.pending_work]
        if missing_call_ids:
            raise RuntimeError(f"Monty requested unknown future call IDs: {missing_call_ids}")

        work_items = [self.pending_work[call_id] for call_id in pending_call_ids]
        if all(isinstance(work_item, ActivityWork) for work_item in work_items):
            return (yield from self._resume_activity_futures(snapshot, pending_call_ids, work_items))

        return (yield from self._resume_single_future(snapshot, pending_call_ids[0]))

    def _resume_activity_futures(
        self,
        snapshot: FutureSnapshot,
        call_ids: list[int],
        work_items: list[ActivityWork | WhenAnyWork],
    ):
        tasks = [work_item.task for work_item in work_items if isinstance(work_item, ActivityWork)]
        try:
            if len(tasks) == 1:
                single_result = yield tasks[0]
                results = [single_result]
            else:
                results = yield self.context.task_all(tasks)
        except Exception as exc:
            resume_results = {call_id: _external_error(exc) for call_id in call_ids}
        else:
            resume_results = {
                call_id: {"return_value": _ensure_json_value(result)}
                for call_id, result in zip(call_ids, results, strict=True)
            }

        for call_id in call_ids:
            self.pending_work.pop(call_id, None)
        return snapshot.resume(resume_results)

    def _resume_single_future(self, snapshot: FutureSnapshot, call_id: int):
        work_item = self.pending_work[call_id]
        if isinstance(work_item, ActivityWork):
            try:
                result = yield work_item.task
            except Exception as exc:
                resume_result = _external_error(exc)
            else:
                resume_result = {"return_value": _ensure_json_value(result)}
        elif isinstance(work_item, WhenAnyWork):
            try:
                winner_task = yield self.context.task_any(work_item.tasks)
                result = yield winner_task
            except Exception as exc:
                resume_result = _external_error(exc)
            else:
                resume_result = {
                    "return_value": {
                        "index": _find_task_index(work_item.tasks, winner_task),
                        "result": _ensure_json_value(result),
                    }
                }
        else:
            raise RuntimeError(f"Unsupported future work item: {type(work_item).__name__}")

        self.pending_work.pop(call_id, None)
        return snapshot.resume({call_id: resume_result})

    def _collect_print(self, stream: str, text: str) -> None:
        if self.print_truncated:
            return
        current_size = sum(len(chunk) for chunk in self.print_chunks)
        remaining = MAX_PRINT_OUTPUT_CHARS - current_size
        if remaining <= 0:
            self.print_truncated = True
            return

        text_value = str(text)
        if len(text_value) > remaining:
            self.print_chunks.append(text_value[:remaining])
            self.print_truncated = True
        else:
            self.print_chunks.append(text_value)

    def _complete(self, progress: MontyComplete) -> dict[str, Any]:
        return {
            "output": _ensure_json_value(progress.output),
            "stdout": "".join(self.print_chunks),
            "metadata": {
                "scheduledActivities": self.scheduled_activity_count,
                "stdoutTruncated": self.print_truncated,
            },
        }


def _build_code(code: str) -> str:
    return f"{MONTY_PRELUDE}\n{code}"


def _parse_activity_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> ActivitySpec:
    if len(args) > 2:
        raise ValueError("call_activity accepts at most two positional arguments: name and input.")

    unexpected_kwargs = set(kwargs) - {"name", "input"}
    if unexpected_kwargs:
        raise ValueError(f"Unsupported call_activity keyword arguments: {sorted(unexpected_kwargs)}")

    if args:
        name = args[0]
    elif "name" in kwargs:
        name = kwargs["name"]
    else:
        raise ValueError("call_activity requires an activity name.")

    if len(args) == 2:
        if "input" in kwargs:
            raise ValueError("call_activity input was provided twice.")
        payload = args[1]
    else:
        payload = kwargs.get("input")

    return _activity_spec(name, payload)


def _parse_when_any_specs(args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[ActivitySpec]:
    if kwargs:
        unexpected_kwargs = set(kwargs) - {"activities"}
        if unexpected_kwargs:
            raise ValueError(f"Unsupported when_any keyword arguments: {sorted(unexpected_kwargs)}")
        raw_specs = kwargs.get("activities")
    elif len(args) == 1:
        raw_specs = args[0]
    else:
        raise ValueError("when_any requires one list of activity specs.")

    if not isinstance(raw_specs, list) or not raw_specs:
        raise ValueError("when_any requires a non-empty list of activity specs.")
    if len(raw_specs) > MAX_ACTIVITY_FAN_OUT:
        raise ValueError(f"when_any supports at most {MAX_ACTIVITY_FAN_OUT} activities.")

    specs: list[ActivitySpec] = []
    for raw_spec in raw_specs:
        if not isinstance(raw_spec, dict):
            raise ValueError("Each when_any activity spec must be a dictionary.")
        activity_name = raw_spec.get("activity", raw_spec.get("name"))
        payload = raw_spec.get("input")
        specs.append(_activity_spec(activity_name, payload))
    return specs


def _activity_spec(name: Any, payload: Any) -> ActivitySpec:
    if not isinstance(name, str) or not name:
        raise ValueError("Activity name must be a non-empty string.")
    if name not in ALLOWED_ACTIVITY_NAMES:
        raise ValueError(f"Activity {name!r} is not allowed.")
    return ActivitySpec(name=name, payload=_ensure_json_value(payload))


def _ensure_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("Non-finite floating point values are not JSON-safe.")
        return value
    if isinstance(value, (list, tuple)):
        return [_ensure_json_value(item) for item in value]
    if isinstance(value, dict):
        clean_value: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Dictionary keys must be strings.")
            clean_value[key] = _ensure_json_value(item)
        return clean_value
    raise ValueError(f"Value of type {type(value).__name__} is not JSON-safe.")


def _external_error(exc: Exception) -> dict[str, str]:
    return {"exc_type": type(exc).__name__, "message": str(exc)}


def _find_task_index(tasks: list[Any], winner_task: Any) -> int:
    for index, task in enumerate(tasks):
        if task == winner_task:
            return index
    return -1