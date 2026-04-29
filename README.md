# Monty Durable Prototype

This is a prototype for exploring dynamic code based orchestrations: the HTTP starter accepts a Python code string, the Durable orchestrator runs that code through Monty, and the bridge maps approved Monty calls to Durable Functions concepts.

The goal is to learn whether Monty can safely drive orchestration logic while Durable Functions remains responsible for durable scheduling, replay, activities, fan-out/fan-in, and external events.

One motivating scenario is AI agents dynamically defining plans or workflows at runtime, then running those workflows with Durable Functions reliability instead of keeping them inside an ephemeral agent process.

## How It Works

1. A client posts Python orchestration code to `POST /api/orchestrators/monty_orchestrator`.
2. The Durable orchestrator passes that code to `run_monty_orchestration(...)`.
3. Monty executes deterministic Python until it reaches an approved external function.
4. The bridge translates that external function into Durable work and resumes Monty with a future.
5. When Durable completes the task or receives the event, the bridge resumes the Monty future with the result.

## Available Monty DSL

The code string is Python, but it is written against this small DSL rather than the raw Durable Functions Python API. For example, Monty code uses `await call_activity(...)`; it does not use `yield context.call_activity(...)` or receive a Durable `context` object directly.

- `await call_activity(name, input=None)` schedules an allowed Durable activity.
- `await when_all([...])` fans in a list of Monty awaitables using `asyncio.gather`, which the bridge maps to Durable `task_all`.
- `await when_any([...])` currently accepts host-managed activity specs only.
- `await wait_for_external_event(name)` waits for a Durable external event and returns the event payload.

Allowed activities in this prototype:

- `echo` for local smoke tests.
- `azure_rest` for read-only Azure Resource Manager GET requests.

## Durable/Monty Execution Model

This prototype follows the same core shape as normal Durable Functions orchestrators, even though the orchestration logic is supplied as dynamic Monty code.

- Monty code expresses deterministic orchestration flow: branching, shaping data, scheduling Durable work through the DSL, and awaiting Durable results.
- Side effects such as network calls and Azure API calls run in activities. The Monty code asks for that work with `call_activity(...)`; the bridge maps it to Durable history-backed tasks.
- The bridge exposes a small allowlisted DSL instead of every possible Python function. That is how dynamic code maps back to Durable concepts in a controlled way.
- Replay still matters. The orchestrator may run the Monty code multiple times, and Durable history is what makes each scheduled task or received event resolve consistently.
- External events use Durable's normal delivery model. Payloads should include IDs if user code needs to deduplicate repeated events.

## Prototype Scope

These are features we have not implemented yet, not inherent limitations of the idea.

- The `azure_rest` activity is read-only and allows only `GET`.
- `when_any` currently accepts host-managed activity specs only. A more natural Python shape like `await when_any([call_activity(...), wait_for_external_event(...)])` is not implemented yet, which means users cannot race already-created activity/event awaitables or pass those awaitables through helper functions before racing them.
- Durable timers and timeout helpers are not implemented yet.
- Sub-orchestrations, Durable entities, `continue_as_new`, custom status, retry policies, and cancellation helpers are not exposed in the Monty DSL yet.
- Replay-safe bridge or activity versions of direct time, random, filesystem, environment, and network APIs are not exposed yet.

## Local Setup

Start the local backend services:

```bash
docker start azurite monty-dts-emulator
```

Start the Functions host from this workspace:

```bash
source .venv/bin/activate
func start
```

For Azure REST scenarios, sign in and make sure the subscription ID in the `.http` files is correct:

```bash
az login
az account show
```

The Durable Task Scheduler dashboard is available at:

```text
http://localhost:8082
```

## HTTP Scenario Files

The guided REST Client scenarios are split by concept under `http/`.

### `http/01-basic.http`

Use this first. It covers:

- Pure Monty code.
- `call_activity("echo", ...)`.
- A rejected activity to show the allowlist failing closed.

### `http/02-when-all.http`

Use this to test fan-out/fan-in. It covers:

- Local echo activity fan-out with `when_all`.
- Azure Function App inventory through `azure_rest`.
- Azure Function App detail fan-out through `when_all`.

### `http/03-when-any.http`

Use this to test the current v1 `when_any` behavior. It covers:

- Host-managed activity specs.
- First-completed result shape.
- The current limitation that loser activities are not canceled.

### `http/04-external-events.http`

Use this to test external events and human approval. It covers:

- A minimal `wait_for_external_event("Approval")` flow.
- Raising an event with `POST /api/orchestrations/{instanceId}/events/{eventName}`.
- A richer human approval scenario that lists up to 20 Azure Function Apps, waits for approval, then fetches detail records only when approved.

The root `test.http` file is now an index that points to these scenario files.

## Useful Verification Commands

Run unit tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Compile the Python files:

```bash
.venv/bin/python -m compileall function_app.py monty_bridge.py azure_rest_activity.py tests/test_monty_bridge.py
```

Check emulator containers and ports:

```bash
docker ps --filter name=azurite --filter name=monty-dts-emulator --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
lsof -nP -iTCP:10000 -iTCP:10001 -iTCP:10002 -iTCP:8080 -iTCP:8082 -sTCP:LISTEN
```
