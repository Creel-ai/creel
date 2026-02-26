/**
 * API client for the Creel dashboard backend.
 */

const BASE = '/api';

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail ?? body.error ?? res.statusText);
  }
  return res.json() as Promise<T>;
}

// ---- Status types ----

export interface DaemonInfo {
  running: boolean;
  pid: number;
  uptime_seconds: number;
  socket: string;
}

export interface AgentInfo {
  name: string;
  model: string;
  provider: string;
}

export interface ChannelInfo {
  name: string;
  enabled: boolean;
  connected: boolean;
}

export interface TaskStats {
  total: number;
  scheduled: number;
}

export interface CronInfo {
  enabled_jobs: number;
  total_jobs: number;
  next_run: string | null;
}

export interface RecentRun {
  task_name: string;
  status: string;
  finished_at: string | null;
  duration_ms: number | null;
}

export interface StatusResponse {
  daemon: DaemonInfo;
  agent: AgentInfo;
  channels: ChannelInfo[];
  tasks: TaskStats;
  cron: CronInfo;
  recent_runs: RecentRun[];
}

// ---- API methods ----

export function fetchStatus(): Promise<StatusResponse> {
  return request<StatusResponse>('/status');
}
