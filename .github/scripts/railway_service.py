#!/usr/bin/env python3
"""Minimal Railway Public API helper for GitHub Actions.

The Railway CLI treats project tokens and account/workspace tokens differently.
This helper uses the GraphQL API directly so scheduled start/stop jobs only need
one GitHub secret: RAILWAY_TOKEN, passed into this script as RAILWAY_API_TOKEN.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


ENDPOINT = "https://backboard.railway.com/graphql/v2"
USER_AGENT = "virtual-trader-railway-scheduler/1.0"


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def graphql(query: str, variables: dict[str, object]) -> dict[str, object]:
    token = require_env("RAILWAY_API_TOKEN")
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"Railway API HTTP {error.code}: {body}") from error

    if payload.get("errors"):
        messages = "; ".join(str(error.get("message", error)) for error in payload["errors"])
        raise SystemExit(f"Railway API error: {messages}")

    return payload["data"]


def service_variables() -> dict[str, str]:
    return {
        "environmentId": require_env("RAILWAY_ENVIRONMENT_ID"),
        "serviceId": require_env("RAILWAY_SERVICE_ID"),
    }


def status() -> None:
    query = """
    query($environmentId: String!, $serviceId: String!) {
      serviceInstance(environmentId: $environmentId, serviceId: $serviceId) {
        serviceId
        environmentId
        numReplicas
        sleepApplication
        latestDeployment { id status }
        activeDeployments { id status }
      }
    }
    """
    data = graphql(query, service_variables())
    print(json.dumps(data["serviceInstance"], indent=2, sort_keys=True))


def scale(replicas: int) -> None:
    mutation = """
    mutation($environmentId: String!, $serviceId: String!, $replicas: Int!) {
      serviceInstanceUpdate(
        environmentId: $environmentId,
        serviceId: $serviceId,
        input: { numReplicas: $replicas }
      )
    }
    """
    variables = service_variables()
    variables["replicas"] = replicas
    graphql(mutation, variables)
    print(f"Railway service replica target set to {replicas}.")
    status()


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"status", "start", "stop"}:
        raise SystemExit("Usage: railway_service.py [status|start|stop]")

    action = sys.argv[1]
    if action == "status":
        status()
    elif action == "start":
        scale(1)
    else:
        scale(0)


if __name__ == "__main__":
    main()
