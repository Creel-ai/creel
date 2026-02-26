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

export function createTask(data: TaskUpdateRequest): Promise<TaskDetail> {
  return request<TaskDetail>('/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export function deleteTask(name: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/tasks/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
}

export function runTask(name: string): Promise<{ run_id: string; task_name: string; status: string }> {
  return request(`/tasks/${encodeURIComponent(name)}/run`, {
    method: 'POST',
  });
}

// ---- Cron types ----

export interface CronJob {
  name: string;
  schedule: string;
  schedule_human: string;
  next_run: string | null;
  last_run: string | null;
  last_status: string | null;
  enabled: boolean;
}

export interface CronRunRecord {
  run_id: string;
  job_name: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  status: string;
  token_usage: unknown;
  error: string | null;
  summary: string | null;
}

export interface CronHistoryResponse {
  runs: CronRunRecord[];
  total: number;
  limit: number;
  offset: number;
}

// ---- Cron API methods ----

export function fetchCronJobs(): Promise<CronJob[]> {
  return request<CronJob[]>('/cron/jobs');
}

export function toggleCronJob(name: string): Promise<{ name: string; enabled: boolean; schedule: string }> {
  return request(`/cron/jobs/${encodeURIComponent(name)}/toggle`, { method: 'POST' });
}

export function fetchCronHistory(params?: {
  job?: string;
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<CronHistoryResponse> {
  const sp = new URLSearchParams();
  if (params?.job) sp.set('job', params.job);
  if (params?.status) sp.set('status', params.status);
  if (params?.limit) sp.set('limit', String(params.limit));
  if (params?.offset) sp.set('offset', String(params.offset));
  const qs = sp.toString();
  return request<CronHistoryResponse>(`/cron/history${qs ? `?${qs}` : ''}`);
}

export function fetchCronRunDetail(runId: string): Promise<CronRunRecord> {
  return request<CronRunRecord>(`/cron/history/${encodeURIComponent(runId)}`);
}

// ---- File browser types ----

export interface FileTreeNode {
  name: string;
  path: string;
  type: 'file' | 'dir';
  size_bytes: number;
  modified_at: string | null;
  children?: FileTreeNode[];
}

export interface FileContent {
  path: string;
  content: string | null;
  binary: boolean;
  size_bytes: number;
  modified_at: string;
}

export interface FileWriteResult {
  path: string;
  size_bytes: number;
  modified_at: string;
}

// ---- File browser API methods ----

export function fetchFileTree(): Promise<FileTreeNode> {
  return request<FileTreeNode>('/files/tree');
}

export function fetchFileContent(filePath: string): Promise<FileContent> {
  return request<FileContent>(`/files/${filePath}`);
}

export function updateFile(filePath: string, content: string): Promise<FileWriteResult> {
  return request<FileWriteResult>(`/files/${filePath}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
}

export function createFile(filePath: string, content: string): Promise<FileWriteResult> {
  return request<FileWriteResult>(`/files/${filePath}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
}

export function deleteFile(filePath: string): Promise<{ status: string; path: string }> {
  return request<{ status: string; path: string }>(`/files/${filePath}`, {
    method: 'DELETE',
  });
}

// ---- Config types ----

export interface ConfigResponse {
  config: Record<string, unknown>;
  raw_yaml: string;
}

export interface ConfigSaveResponse {
  status: string;
  config: Record<string, unknown>;
  raw_yaml: string;
}

// ---- Config API methods ----

export function fetchConfig(): Promise<ConfigResponse> {
  return request<ConfigResponse>('/config');
}

export function fetchConfigSchema(): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>('/config/schema');
}

export function updateConfig(data: { json?: Record<string, unknown>; raw_yaml?: string }): Promise<ConfigSaveResponse> {
  return request<ConfigSaveResponse>('/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export function applyConfig(): Promise<{ status: string }> {
  return request<{ status: string }>('/config/apply', { method: 'POST' });
}

// ---- Logs types ----

export interface LogEntry {
  timestamp: string;
  level: string;
  module: string;
  message: string;
}

export interface LogsRecentResponse {
  lines: LogEntry[];
  total: number;
}

// ---- Logs API methods ----

export function fetchRecentLogs(params?: {
  limit?: number;
  level?: string;
}): Promise<LogsRecentResponse> {
  const sp = new URLSearchParams();
  if (params?.limit) sp.set('limit', String(params.limit));
  if (params?.level) sp.set('level', params.level);
  const qs = sp.toString();
  return request<LogsRecentResponse>(`/logs/recent${qs ? `?${qs}` : ''}`);
}

/**
 * Create a WebSocket connection to stream logs.
 * Returns the WebSocket instance. The caller manages the connection lifecycle.
 */
export function createLogsWebSocket(): WebSocket {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host;
  return new WebSocket(`${proto}//${host}/ws/logs`);
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
