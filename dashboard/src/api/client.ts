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

// ---- Task types ----

export interface TaskSummary {
  name: string;
  description: string;
  schedule: string;
  enabled: boolean;
  last_modified: string | null;
  file_path: string;
}

export interface TaskDetail {
  name: string;
  description: string;
  schedule: string;
  prompt: string;
  output_type: string;
  output_to: string;
  model: string;
  max_tokens: number;
  mode: string;
  enabled: boolean;
  raw_yaml: string;
  file_path: string;
  last_modified: string | null;
}

export interface TaskUpdateRequest {
  name: string;
  description?: string;
  schedule?: string;
  prompt?: string;
  output_type?: string;
  output_to?: string;
  model?: string;
  max_tokens?: number;
  mode?: string;
  enabled?: boolean;
  raw_yaml?: string | null;
}

// ---- API methods ----

export function fetchStatus(): Promise<StatusResponse> {
  return request<StatusResponse>('/status');
}

export function fetchTasks(): Promise<TaskSummary[]> {
  return request<TaskSummary[]>('/tasks');
}

export function fetchTaskDetail(name: string): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${encodeURIComponent(name)}`);
}

export function updateTask(name: string, data: TaskUpdateRequest): Promise<unknown> {
  return request(`/tasks/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

/**
 * Toggle a task's enabled field by reading the raw YAML, modifying it, and writing back.
 */
export async function toggleTaskEnabled(name: string, enabled: boolean): Promise<void> {
  const detail = await fetchTaskDetail(name);
  let yaml = detail.raw_yaml;
  // Update or add the enabled field in the YAML
  if (/^enabled\s*:/m.test(yaml)) {
    yaml = yaml.replace(/^enabled\s*:.*/m, `enabled: ${enabled}`);
  } else {
    yaml = yaml.trimEnd() + `\nenabled: ${enabled}\n`;
  }
  await updateTask(name, { name, raw_yaml: yaml });
}
