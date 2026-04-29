from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from monty_bridge import run_monty_orchestration


@dataclass(frozen=True)
class FakeTask:
    kind: str
    name: str | None = None
    payload: Any = None


class FakeDurableContext:
    def call_activity(self, name: str, payload: Any = None) -> FakeTask:
        return FakeTask("activity", name, payload)

    def wait_for_external_event(self, name: str) -> FakeTask:
        return FakeTask("event", name)

    def task_all(self, tasks: list[FakeTask]) -> FakeTask:
        return FakeTask("all", payload=tasks)

    def task_any(self, tasks: list[FakeTask]) -> FakeTask:
        return FakeTask("any", payload=tasks)


class MontyBridgeTests(unittest.TestCase):
    def test_pure_code_completes(self) -> None:
        result = drive("1 + 2")

        self.assertEqual(result["output"], 3)
        self.assertEqual(result["metadata"]["scheduledActivities"], 0)

    def test_await_call_activity(self) -> None:
        result = drive('result = await call_activity("echo", {"hello": "world"})\nresult')

        self.assertEqual(result["output"], {"activity": "echo", "input": {"hello": "world"}})
        self.assertEqual(result["metadata"]["scheduledActivities"], 1)

    def test_when_all_uses_durable_task_all(self) -> None:
        result = drive(
            'tasks = [call_activity("echo", {"i": 1}), call_activity("echo", {"i": 2})]\n'
            "await when_all(tasks)"
        )

        self.assertEqual(
            result["output"],
            [
                {"activity": "echo", "input": {"i": 1}},
                {"activity": "echo", "input": {"i": 2}},
            ],
        )
        self.assertEqual(result["metadata"]["scheduledActivities"], 2)

    def test_when_any_returns_first_completed_result(self) -> None:
        result = drive(
            'await when_any([{"activity": "echo", "input": {"i": 1}}, '
            '{"activity": "echo", "input": {"i": 2}}])'
        )

        self.assertEqual(result["output"], {"index": 0, "result": {"activity": "echo", "input": {"i": 1}}})
        self.assertEqual(result["metadata"]["scheduledActivities"], 2)

    def test_unknown_activity_is_rejected_in_monty(self) -> None:
        with self.assertRaisesRegex(Exception, "not allowed"):
            drive('await call_activity("not_allowed", {})')

    def test_await_wait_for_external_event(self) -> None:
        result = drive('event = await wait_for_external_event("Approval")\nevent')

        self.assertEqual(result["output"], {"event": "Approval", "payload": {"approved": True}})
        self.assertEqual(result["metadata"]["scheduledActivities"], 0)

    def test_wait_for_external_event_accepts_name_keyword(self) -> None:
        result = drive('await wait_for_external_event(name="Approval")')

        self.assertEqual(result["output"], {"event": "Approval", "payload": {"approved": True}})

    def test_wait_for_external_event_normalizes_json_payload_text(self) -> None:
        result = drive('await wait_for_external_event("JsonStringPayload")')

        self.assertEqual(result["output"], {"approved": True, "reviewer": "fake-runtime"})

    def test_wait_for_external_event_rejects_invalid_name(self) -> None:
        with self.assertRaisesRegex(Exception, "non-empty string"):
            drive("await wait_for_external_event(42)")

    def test_wait_for_external_event_rejects_unexpected_keyword(self) -> None:
        with self.assertRaisesRegex(Exception, "Unsupported wait_for_external_event keyword"):
            drive('await wait_for_external_event(event="Approval")')

    def test_when_all_can_wait_for_multiple_external_events(self) -> None:
        result = drive(
            'events = [wait_for_external_event("Approval"), wait_for_external_event("Escalation")]\n'
            "await when_all(events)"
        )

        self.assertEqual(
            result["output"],
            [
                {"event": "Approval", "payload": {"approved": True}},
                {"event": "Escalation", "payload": {"approved": True}},
            ],
        )

    def test_when_all_can_wait_for_activity_and_external_event(self) -> None:
        result = drive(
            'tasks = [call_activity("echo", {"i": 1}), wait_for_external_event("Approval")]\n'
            "await when_all(tasks)"
        )

        self.assertEqual(
            result["output"],
            [
                {"activity": "echo", "input": {"i": 1}},
                {"event": "Approval", "payload": {"approved": True}},
            ],
        )


def drive(code: str) -> dict[str, Any]:
    context = FakeDurableContext()
    runner = run_monty_orchestration(context, code)

    try:
        yielded = next(runner)
        while True:
            yielded = runner.send(resolve_fake_task(yielded))
    except StopIteration as stop:
        return stop.value


def resolve_fake_task(task: FakeTask) -> Any:
    if task.kind == "activity":
        return {"activity": task.name, "input": task.payload}
    if task.kind == "event":
        if task.name == "JsonStringPayload":
            return '{"approved": true, "reviewer": "fake-runtime"}'
        return {"event": task.name, "payload": {"approved": True}}
    if task.kind == "all":
        return [resolve_fake_task(child_task) for child_task in task.payload]
    if task.kind == "any":
        return task.payload[0]
    raise AssertionError(f"Unexpected fake task: {task!r}")


if __name__ == "__main__":
    unittest.main()