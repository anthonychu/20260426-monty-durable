from __future__ import annotations

import azure.durable_functions as df
import azure.functions as func

from azure_rest_activity import azure_rest as execute_azure_rest
from monty_bridge import run_monty_orchestration


app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="orchestrators/{functionName}", methods=["POST"])
@app.durable_client_input(client_name="client")
async def start_orchestration(req: func.HttpRequest, client):
    function_name = req.route_params.get("functionName")
    if function_name != "monty_orchestrator":
        return func.HttpResponse(f"Unknown orchestrator: {function_name}", status_code=404)

    try:
        code = _extract_code(req)
    except ValueError as exc:
        return func.HttpResponse(str(exc), status_code=400)

    instance_id = await client.start_new(function_name, None, code)
    return client.create_check_status_response(req, instance_id)


@app.orchestration_trigger(context_name="context")
def monty_orchestrator(context):
    code = context.get_input()
    return (yield from run_monty_orchestration(context, code))


@app.activity_trigger(input_name="params")
async def azure_rest(params):
    return await execute_azure_rest(params)


@app.activity_trigger(input_name="payload")
def echo(payload):
    return payload


def _extract_code(req: func.HttpRequest) -> str:
    body = req.get_body()
    if not body:
        raise ValueError("Request body must contain a Python code string or a JSON object with a 'code' string.")

    try:
        payload = req.get_json()
    except ValueError:
        code = body.decode("utf-8")
    else:
        if isinstance(payload, str):
            code = payload
        elif isinstance(payload, dict) and isinstance(payload.get("code"), str):
            code = payload["code"]
        else:
            raise ValueError("JSON request body must be a string or an object with a 'code' string.")

    if not code.strip():
        raise ValueError("The Python code string cannot be empty.")
    return code