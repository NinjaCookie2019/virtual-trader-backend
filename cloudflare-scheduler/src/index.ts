type SchedulerAction = "start" | "stop" | "renew" | "status";

interface Env {
  BACKEND_URL: string;
  GITHUB_OWNER: string;
  GITHUB_REPO: string;
  GITHUB_REF: string;
  GITHUB_WORKFLOW: string;
  GITHUB_TOKEN?: string;
  RAILWAY_API_TOKEN?: string;
  RAILWAY_ENVIRONMENT_ID: string;
  RAILWAY_SERVICE_ID: string;
  SCHEDULER_SECRET?: string;
  FORCE_ACTION?: string;
  FORCE_ACTION_UNTIL_UTC?: string;
  AUTO_STOP_AFTER_MARKET?: string;
}

interface SchedulerDecision {
  action: SchedulerAction;
  istMinutes: number;
  istTimestamp: string;
}

const RAILWAY_GRAPHQL_ENDPOINT = "https://backboard.railway.com/graphql/v2";

export default {
  async scheduled(controller: ScheduledController, env: Env, ctx: ExecutionContext) {
    ctx.waitUntil(
      runScheduler(env, determineAction(new Date(), env), "cron", controller.cron).then((result) => {
        console.log(JSON.stringify(result));
      }),
    );
  },

  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return jsonResponse({ ok: true, service: "virtual-trader-scheduler" });
    }

    if (url.pathname !== "/run") {
      return jsonResponse({ error: "Not found" }, 404);
    }

    if (request.method !== "POST") {
      return jsonResponse({ error: "Use POST /run?action=start|stop|renew|status" }, 405);
    }

    const unauthorized = authorize(request, env);
    if (unauthorized) {
      return unauthorized;
    }

    const requestedAction = url.searchParams.get("action");
    const decision = requestedAction
      ? {
          action: parseAction(requestedAction),
          istMinutes: getIstMinutes(new Date()),
          istTimestamp: formatIst(new Date()),
        }
      : determineAction(new Date(), env);

    return jsonResponse(await runScheduler(env, decision, "manual"));
  },
};

function determineAction(now: Date, env: Env): SchedulerDecision {
  const istMinutes = getIstMinutes(now);
  let action: SchedulerAction = "status";
  const forceAction = parseOptionalAction(env.FORCE_ACTION);
  const forceUntil = env.FORCE_ACTION_UNTIL_UTC ? Date.parse(env.FORCE_ACTION_UNTIL_UTC) : Number.NaN;

  if (forceAction && Number.isFinite(forceUntil) && now.getTime() <= forceUntil) {
    return {
      action: forceAction,
      istMinutes,
      istTimestamp: formatIst(now),
    };
  }

  if (istMinutes >= 855 && istMinutes <= 920) {
    action = "start";
  } else if (istMinutes >= 1130 && istMinutes <= 1230) {
    action = "renew";
  } else if (istMinutes >= 1530 && istMinutes <= 1539) {
    action = "renew";
  } else if (istMinutes >= 1540 && istMinutes <= 1555 && shouldAutoStopAfterMarket(env)) {
    action = "stop";
  }

  return {
    action,
    istMinutes,
    istTimestamp: formatIst(now),
  };
}

function shouldAutoStopAfterMarket(env: Env): boolean {
  const value = env.AUTO_STOP_AFTER_MARKET?.trim().toLowerCase();
  return value === "1" || value === "true" || value === "yes";
}

async function runScheduler(
  env: Env,
  decision: SchedulerDecision,
  source: "cron" | "manual",
  cron?: string,
) {
  const context = {
    action: decision.action,
    source,
    cron: cron ?? null,
    istTimestamp: decision.istTimestamp,
    istMinutes: decision.istMinutes,
  };

  if (decision.action === "status") {
    if (source === "cron") {
      return {
        ...context,
        backend: null,
        railway: null,
        dispatched: false,
        message: "Outside trading scheduler windows; cron no-op to avoid unnecessary calls.",
      };
    }

    return {
      ...context,
      backend: await readBackendState(env),
      railway: env.RAILWAY_API_TOKEN ? await railwayStatus(env) : null,
      dispatched: false,
      message: "Outside trading scheduler windows; no start/stop action sent.",
    };
  }

  if (decision.action === "start") {
    const backend = await readBackendState(env);
    if (backend.healthy) {
      return {
        ...context,
        backend,
        dispatched: false,
        message: "Backend already healthy; skipped start action.",
      };
    }
  }

  if (decision.action === "renew") {
    let backend = await readBackendState(env);
    if (!backend.healthy && env.RAILWAY_API_TOKEN) {
      await railwayStart(env);
      backend = await waitForBackend(env);
    }
    if (!backend.healthy) {
      await dispatchGithubWorkflow(env, "start");
      return {
        ...context,
        backend,
        dispatched: true,
        message: "Backend was asleep; start dispatched. Renewal will retry on the next cron tick.",
      };
    }

    const tokenValidUntil = getTokenValidUntil(backend.state);
    const isPreShutdownRenewal = decision.istMinutes >= 1530 && decision.istMinutes <= 1539;
    if (!isPreShutdownRenewal && tokenValidUntil && tokenValidUntil.getTime() - Date.now() > 6 * 60 * 60 * 1000) {
      return {
        ...context,
        backend,
        renewal: null,
        dispatched: false,
        message: `Skipped renewal; token is already valid until ${tokenValidUntil.toISOString()}.`,
      };
    }

    const renewal = await renewBackendToken(env);
    return {
      ...context,
      backend,
      renewal,
      dispatched: false,
      message: "Dhan token renewal requested before Railway shutdown window.",
    };
  }

  if (env.RAILWAY_API_TOKEN) {
    const railway = decision.action === "start" ? await railwayStart(env) : await railwayStop(env);
    return {
      ...context,
      railway,
      dispatched: false,
      message: `Railway ${decision.action} requested directly from Cloudflare Worker.`,
    };
  }

  await dispatchGithubWorkflow(env, decision.action);
  return {
    ...context,
    dispatched: true,
    message: `GitHub workflow_dispatch sent with action=${decision.action}.`,
  };
}

function getTokenValidUntil(state: unknown): Date | null {
  if (!state || typeof state !== "object") {
    return null;
  }
  const connections = (state as { connections?: unknown }).connections;
  if (!connections || typeof connections !== "object") {
    return null;
  }
  const rawValue = (connections as { token_valid_until?: unknown }).token_valid_until;
  if (typeof rawValue !== "string") {
    return null;
  }
  const timestamp = Date.parse(rawValue);
  return Number.isFinite(timestamp) ? new Date(timestamp) : null;
}

async function readBackendState(env: Env) {
  const health = await fetchWithTimeout(`${env.BACKEND_URL}/api/health`, 8000).catch((error) => {
    return {
      ok: false,
      status: 0,
      error: error instanceof Error ? error.message : String(error),
    };
  });

  if (!health.ok) {
    return {
      healthy: false,
      status: health.status,
      error: "error" in health ? health.error : null,
      state: null,
    };
  }

  const state = await fetchWithTimeout(`${env.BACKEND_URL}/api/state`, 8000);
  return {
    healthy: true,
    status: state.status,
    state: state.ok ? await state.json().catch(() => null) : null,
  };
}

async function waitForBackend(env: Env) {
  let backend = await readBackendState(env);
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (backend.healthy) {
      return backend;
    }
    await delay(10_000);
    backend = await readBackendState(env);
  }
  return backend;
}

async function renewBackendToken(env: Env) {
  if (!env.SCHEDULER_SECRET) {
    throw new Error("Missing SCHEDULER_SECRET secret for backend token renewal.");
  }

  const response = await fetchWithTimeout(`${env.BACKEND_URL}/api/admin/renew-token`, 20_000, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.SCHEDULER_SECRET}`,
    },
  });
  const body = await response.text();
  if (!response.ok) {
    throw new Error(`Backend token renewal failed: HTTP ${response.status} ${body.slice(0, 500)}`);
  }
  return JSON.parse(body) as unknown;
}

async function dispatchGithubWorkflow(env: Env, action: SchedulerAction) {
  if (!env.GITHUB_TOKEN) {
    throw new Error("Missing GITHUB_TOKEN secret or RAILWAY_API_TOKEN secret.");
  }

  const endpoint = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/${env.GITHUB_WORKFLOW}/dispatches`;
  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "virtual-trader-cloudflare-scheduler",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({
      ref: env.GITHUB_REF,
      inputs: { action },
    }),
  });

  if (response.status !== 204) {
    const body = await response.text();
    throw new Error(`GitHub dispatch failed: HTTP ${response.status} ${body.slice(0, 500)}`);
  }
}

async function railwayStatus(env: Env) {
  const query = `
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
  `;
  const data = await railwayGraphql(env, query, railwayVariables(env));
  return data.serviceInstance;
}

async function railwayStart(env: Env) {
  const mutation = `
    mutation($environmentId: String!, $serviceId: String!) {
      serviceInstanceDeploy(
        environmentId: $environmentId,
        serviceId: $serviceId,
        latestCommit: true
      )
    }
  `;
  await railwayGraphql(env, mutation, railwayVariables(env));
  return railwayStatus(env);
}

async function railwayStop(env: Env) {
  const instance = await railwayStatus(env);
  const activeDeployments = Array.isArray(instance.activeDeployments) ? instance.activeDeployments : [];
  const mutation = "mutation($id: String!) { deploymentStop(id: $id) }";
  const stopped: string[] = [];

  for (const deployment of activeDeployments) {
    if (!deployment?.id) {
      continue;
    }
    await railwayGraphql(env, mutation, { id: String(deployment.id) });
    stopped.push(String(deployment.id));
  }

  return {
    stopped,
    current: await railwayStatus(env),
  };
}

async function railwayGraphql(env: Env, query: string, variables: Record<string, unknown>) {
  if (!env.RAILWAY_API_TOKEN) {
    throw new Error("Missing RAILWAY_API_TOKEN secret.");
  }

  const response = await fetch(RAILWAY_GRAPHQL_ENDPOINT, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.RAILWAY_API_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "virtual-trader-cloudflare-scheduler/1.0",
    },
    body: JSON.stringify({ query, variables }),
  });

  const payload = await response.json<Record<string, unknown>>();
  if (!response.ok || payload.errors) {
    throw new Error(`Railway API failed: HTTP ${response.status} ${JSON.stringify(payload).slice(0, 500)}`);
  }

  return payload.data as Record<string, any>;
}

function railwayVariables(env: Env) {
  return {
    environmentId: env.RAILWAY_ENVIRONMENT_ID,
    serviceId: env.RAILWAY_SERVICE_ID,
  };
}

function authorize(request: Request, env: Env) {
  if (!env.SCHEDULER_SECRET) {
    return null;
  }

  const expected = `Bearer ${env.SCHEDULER_SECRET}`;
  if (request.headers.get("Authorization") !== expected) {
    return jsonResponse({ error: "Unauthorized" }, 401);
  }

  return null;
}

async function fetchWithTimeout(url: string, timeoutMs: number, init?: RequestInit) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort("timeout"), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

function parseAction(value: string): SchedulerAction {
  if (value === "start" || value === "stop" || value === "renew" || value === "status") {
    return value;
  }

  throw new Error("Invalid action. Use start, stop, renew, or status.");
}

function parseOptionalAction(value: string | undefined): SchedulerAction | null {
  if (!value) {
    return null;
  }

  return parseAction(value);
}

function getIstMinutes(date: Date) {
  const ist = new Date(date.getTime() + 5.5 * 60 * 60 * 1000);
  return ist.getUTCHours() * 100 + ist.getUTCMinutes();
}

function formatIst(date: Date) {
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "Asia/Kolkata",
  }).format(date);
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

function delay(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
