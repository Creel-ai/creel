import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import AddIcon from '@mui/icons-material/Add';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import FilterListIcon from '@mui/icons-material/FilterList';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Checkbox from '@mui/material/Checkbox';
import Chip from '@mui/material/Chip';
import Collapse from '@mui/material/Collapse';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import FormControlLabel from '@mui/material/FormControlLabel';
import IconButton from '@mui/material/IconButton';
import MenuItem from '@mui/material/MenuItem';
import Select from '@mui/material/Select';
import Skeleton from '@mui/material/Skeleton';
import Snackbar from '@mui/material/Snackbar';
import Stack from '@mui/material/Stack';
import Switch from '@mui/material/Switch';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableContainer from '@mui/material/TableContainer';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';
import TableSortLabel from '@mui/material/TableSortLabel';
import TextField from '@mui/material/TextField';
import ToggleButton from '@mui/material/ToggleButton';
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup';
import Typography from '@mui/material/Typography';

import type { CronJob, CronRunRecord, TaskUpdateRequest } from '../api/client';
import {
  createTask,
  fetchCronHistory,
  fetchCronJobs,
  toggleCronJob,
} from '../api/client';

// ---- Helpers ----

function relativeTime(iso: string | null): string {
  if (!iso) return '-';
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 0) return 'just now';
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

function formatDuration(ms: number | null): string {
  if (ms == null) return '-';
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  return `${m}m ${rem}s`;
}

function statusColor(status: string | null): 'success' | 'error' | 'warning' | 'default' {
  switch (status) {
    case 'success': return 'success';
    case 'failed': return 'error';
    case 'timeout': return 'warning';
    default: return 'default';
  }
}

// ---- Schedule presets for new job dialog ----

const SCHEDULE_PRESETS = [
  { label: 'Custom', value: '' },
  { label: 'Every minute', value: '* * * * *' },
  { label: 'Every 5 minutes', value: '*/5 * * * *' },
  { label: 'Hourly', value: '0 * * * *' },
  { label: 'Daily at 9:00 AM', value: '0 9 * * *' },
  { label: 'Daily at midnight', value: '0 0 * * *' },
  { label: 'Weekly (Monday 9 AM)', value: '0 9 * * 1' },
];

function humanSchedule(cron: string): string {
  if (!cron) return '';
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return cron;
  const [min, hour, dom, mon, dow] = parts;
  if (min === '*' && hour === '*') return 'Every minute';
  const mMatch = min.match(/^\*\/(\d+)$/);
  if (mMatch && hour === '*') return `Every ${mMatch[1]} minutes`;
  if (hour === '*') return `Every hour at :${min.padStart(2, '0')}`;
  if (dom === '*' && mon === '*' && dow === '*') return `Daily at ${hour}:${min.padStart(2, '0')}`;
  if (dow !== '*' && dom === '*' && mon === '*') {
    const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const dayName = days[Number(dow)] ?? dow;
    return `Weekly (${dayName}) at ${hour}:${min.padStart(2, '0')}`;
  }
  return cron;
}

// ---- Sort types ----

type JobSortKey = 'next_run' | 'name';
type SortDir = 'asc' | 'desc';

function compareJobs(a: CronJob, b: CronJob, key: JobSortKey): number {
  switch (key) {
    case 'name':
      return a.name.localeCompare(b.name);
    case 'next_run':
      return (a.next_run ?? '').localeCompare(b.next_run ?? '');
  }
}

// ---- Loading skeleton ----

function LoadingSkeleton() {
  return (
    <Box>
      <Skeleton width={200} height={40} sx={{ mb: 2 }} />
      <Skeleton width="100%" height={48} />
      {[1, 2, 3].map((i) => (
        <Skeleton key={i} width="100%" height={52} />
      ))}
      <Skeleton width={200} height={40} sx={{ mt: 4, mb: 2 }} />
      <Skeleton width="100%" height={48} />
      {[1, 2, 3].map((i) => (
        <Skeleton key={i} width="100%" height={52} />
      ))}
    </Box>
  );
}

// ---- Expanded history row ----

function HistoryDetail({ run }: { run: CronRunRecord }) {
  return (
    <Box sx={{ px: 2, py: 1 }}>
      <Stack spacing={1}>
        <Typography variant="body2" color="text.secondary">
          <strong>Run ID:</strong> {run.run_id}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          <strong>Started:</strong> {run.started_at ? new Date(run.started_at).toLocaleString() : '-'}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          <strong>Finished:</strong> {run.finished_at ? new Date(run.finished_at).toLocaleString() : '-'}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          <strong>Duration:</strong> {formatDuration(run.duration_ms)}
        </Typography>
        {run.token_usage != null && (
          <Typography variant="body2" color="text.secondary">
            <strong>Tokens:</strong> {JSON.stringify(run.token_usage)}
          </Typography>
        )}
        {run.summary && (
          <Box>
            <Typography variant="body2" color="text.secondary" fontWeight={500}>
              Summary:
            </Typography>
            <Typography
              variant="body2"
              sx={{
                whiteSpace: 'pre-wrap',
                bgcolor: 'action.hover',
                p: 1,
                borderRadius: 1,
                mt: 0.5,
                maxHeight: 200,
                overflow: 'auto',
              }}
            >
              {run.summary}
            </Typography>
          </Box>
        )}
        {run.error && (
          <Box>
            <Typography variant="body2" color="error" fontWeight={500}>
              Error:
            </Typography>
            <Typography
              variant="body2"
              color="error"
              sx={{
                whiteSpace: 'pre-wrap',
                bgcolor: 'action.hover',
                p: 1,
                borderRadius: 1,
                mt: 0.5,
                fontFamily: 'monospace',
                fontSize: '0.75rem',
                maxHeight: 200,
                overflow: 'auto',
              }}
            >
              {run.error}
            </Typography>
          </Box>
        )}
      </Stack>
    </Box>
  );
}

// ---- New Job Dialog ----

interface NewJobForm {
  name: string;
  scheduleType: 'preset' | 'cron';
  presetValue: string;
  cronExpression: string;
  prompt: string;
  model: string;
  timeout: number;
  enabled: boolean;
}

function emptyJobForm(): NewJobForm {
  return {
    name: '',
    scheduleType: 'preset',
    presetValue: '0 9 * * *',
    cronExpression: '',
    prompt: '',
    model: '',
    timeout: 0,
    enabled: true,
  };
}

function NewJobDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (name: string) => void;
}) {
  const [form, setForm] = useState<NewJobForm>(emptyJobForm());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nameError, setNameError] = useState('');

  const SLUG_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;

  const updateField = <K extends keyof NewJobForm>(key: K, value: NewJobForm[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const schedule = form.scheduleType === 'cron' ? form.cronExpression : form.presetValue;

  const handleCreate = async () => {
    setError(null);
    let valid = true;
    if (!SLUG_PATTERN.test(form.name)) {
      setNameError('Lowercase letters, digits, hyphens, underscores. Must start with letter/digit.');
      valid = false;
    } else {
      setNameError('');
    }
    if (!form.prompt.trim()) {
      setError('Prompt is required');
      valid = false;
    }
    if (!schedule.trim()) {
      setError('Schedule is required');
      valid = false;
    }
    if (!valid) return;

    setSaving(true);
    try {
      const lines = [`name: ${form.name}`, `schedule: "${schedule}"`, `prompt: |`];
      for (const line of form.prompt.split('\n')) {
        lines.push(`  ${line}`);
      }
      lines.push(`output:\n  type: stdout\n  to: ""`);
      if (form.model) lines.push(`llm:\n  model: ${form.model}`);
      if (form.timeout > 0) lines.push(`timeout: ${form.timeout}`);
      if (!form.enabled) lines.push('enabled: false');
      const raw_yaml = lines.join('\n') + '\n';

      const data: TaskUpdateRequest = { name: form.name, raw_yaml };
      await createTask(data);
      onCreated(form.name);
      setForm(emptyJobForm());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Create failed');
    } finally {
      setSaving(false);
    }
  };

  const handleClose = () => {
    setForm(emptyJobForm());
    setError(null);
    setNameError('');
    onClose();
  };

  return (
    <Dialog open={open} onClose={handleClose} maxWidth="sm" fullWidth>
      <DialogTitle>New Cron Job</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {error && <Alert severity="error">{error}</Alert>}
          <TextField
            label="Name"
            value={form.name}
            onChange={(e) => updateField('name', e.target.value)}
            required
            error={!!nameError}
            helperText={nameError || 'Lowercase letters, digits, hyphens, underscores'}
            size="small"
            fullWidth
          />
          <Stack spacing={1}>
            <Select
              value={form.scheduleType}
              onChange={(e) => updateField('scheduleType', e.target.value as 'preset' | 'cron')}
              size="small"
              fullWidth
            >
              <MenuItem value="preset">Schedule preset</MenuItem>
              <MenuItem value="cron">Cron expression</MenuItem>
            </Select>
            {form.scheduleType === 'preset' ? (
              <Select
                value={form.presetValue}
                onChange={(e) => updateField('presetValue', e.target.value)}
                size="small"
                fullWidth
              >
                {SCHEDULE_PRESETS.filter((p) => p.value).map((p) => (
                  <MenuItem key={p.value} value={p.value}>
                    {p.label}
                  </MenuItem>
                ))}
              </Select>
            ) : (
              <TextField
                label="Cron expression"
                value={form.cronExpression}
                onChange={(e) => updateField('cronExpression', e.target.value)}
                size="small"
                fullWidth
                helperText={form.cronExpression ? humanSchedule(form.cronExpression) : 'e.g. 0 9 * * *'}
                placeholder="* * * * *"
              />
            )}
          </Stack>
          <TextField
            label="Prompt"
            value={form.prompt}
            onChange={(e) => updateField('prompt', e.target.value)}
            required
            multiline
            rows={4}
            size="small"
            fullWidth
          />
          <TextField
            label="Model override"
            value={form.model}
            onChange={(e) => updateField('model', e.target.value)}
            size="small"
            fullWidth
            helperText="Leave empty to use agent default"
          />
          <TextField
            label="Timeout (seconds)"
            value={form.timeout || ''}
            onChange={(e) => updateField('timeout', Number(e.target.value) || 0)}
            type="number"
            size="small"
            sx={{ width: 200 }}
            helperText="0 = no timeout"
          />
          <FormControlLabel
            control={
              <Checkbox
                checked={form.enabled}
                onChange={(e) => updateField('enabled', e.target.checked)}
              />
            }
            label="Enabled"
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose} disabled={saving}>
          Cancel
        </Button>
        <Button onClick={handleCreate} variant="contained" disabled={saving}>
          {saving ? 'Creating…' : 'Create'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

// ---- Main component ----

export default function CronPage() {
  const mountedRef = useRef(true);

  // Job list state
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [jobsLoading, setJobsLoading] = useState(true);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [jobSearch, setJobSearch] = useState('');
  const [jobFilter, setJobFilter] = useState<'all' | 'enabled' | 'disabled'>('all');
  const [jobSortKey, setJobSortKey] = useState<JobSortKey>('next_run');
  const [jobSortDir, setJobSortDir] = useState<SortDir>('asc');
  const [toggling, setToggling] = useState<Set<string>>(new Set());
  const [selectedJob, setSelectedJob] = useState<string | null>(null);

  // History state
  const [historyRuns, setHistoryRuns] = useState<CronRunRecord[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyStatusFilter, setHistoryStatusFilter] = useState<string>('all');
  const [expandedRun, setExpandedRun] = useState<string | null>(null);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);

  // Dialog state
  const [newJobOpen, setNewJobOpen] = useState(false);
  const [snackbar, setSnackbar] = useState<string | null>(null);

  const HISTORY_LIMIT = 50;

  // Load jobs
  const loadJobs = useCallback(async () => {
    try {
      const list = await fetchCronJobs();
      if (mountedRef.current) {
        setJobs(list);
        setJobsError(null);
      }
    } catch (err) {
      if (mountedRef.current) {
        setJobsError(err instanceof Error ? err.message : 'Failed to fetch cron jobs');
      }
    } finally {
      if (mountedRef.current) setJobsLoading(false);
    }
  }, []);

  // Load history
  const loadHistory = useCallback(async (append = false) => {
    const offset = append ? historyOffset : 0;
    if (append) setLoadingMore(true);
    else setHistoryLoading(true);

    try {
      const resp = await fetchCronHistory({
        job: selectedJob ?? undefined,
        status: historyStatusFilter === 'all' ? undefined : historyStatusFilter,
        limit: HISTORY_LIMIT,
        offset,
      });
      if (mountedRef.current) {
        if (append) {
          setHistoryRuns((prev) => [...prev, ...resp.runs]);
        } else {
          setHistoryRuns(resp.runs);
        }
        setHistoryTotal(resp.total);
        setHistoryError(null);
      }
    } catch (err) {
      if (mountedRef.current) {
        setHistoryError(err instanceof Error ? err.message : 'Failed to fetch history');
      }
    } finally {
      if (mountedRef.current) {
        setHistoryLoading(false);
        setLoadingMore(false);
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedJob, historyStatusFilter, historyOffset]);

  useEffect(() => {
    mountedRef.current = true;
    loadJobs();
    return () => { mountedRef.current = false; };
  }, [loadJobs]);

  // Reload history when filters change
  useEffect(() => {
    setHistoryOffset(0);
    setExpandedRun(null);
    loadHistory(false);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedJob, historyStatusFilter]);

  // Handle job sort
  const handleJobSort = (key: JobSortKey) => {
    if (jobSortKey === key) {
      setJobSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setJobSortKey(key);
      setJobSortDir('asc');
    }
  };

  // Handle toggle
  const handleToggle = async (job: CronJob) => {
    setToggling((prev) => new Set(prev).add(job.name));
    try {
      const result = await toggleCronJob(job.name);
      setJobs((prev) =>
        prev.map((j) => (j.name === job.name ? { ...j, enabled: result.enabled } : j)),
      );
    } catch (err) {
      setJobsError(err instanceof Error ? err.message : 'Failed to toggle job');
    } finally {
      setToggling((prev) => {
        const next = new Set(prev);
        next.delete(job.name);
        return next;
      });
    }
  };

  // Handle job click — filter history
  const handleJobClick = (jobName: string) => {
    setSelectedJob((prev) => (prev === jobName ? null : jobName));
  };

  // Handle load more
  const handleLoadMore = () => {
    const newOffset = historyOffset + HISTORY_LIMIT;
    setHistoryOffset(newOffset);
    // We need to manually trigger with the new offset
    setLoadingMore(true);
    fetchCronHistory({
      job: selectedJob ?? undefined,
      status: historyStatusFilter === 'all' ? undefined : historyStatusFilter,
      limit: HISTORY_LIMIT,
      offset: newOffset,
    })
      .then((resp) => {
        if (mountedRef.current) {
          setHistoryRuns((prev) => [...prev, ...resp.runs]);
          setHistoryTotal(resp.total);
        }
      })
      .catch((err) => {
        if (mountedRef.current) {
          setHistoryError(err instanceof Error ? err.message : 'Failed to load more');
        }
      })
      .finally(() => {
        if (mountedRef.current) setLoadingMore(false);
      });
  };

  // Handle history row expand
  const handleExpandRun = (runId: string) => {
    setExpandedRun((prev) => (prev === runId ? null : runId));
  };

  // Handle new job created
  const handleJobCreated = (name: string) => {
    setNewJobOpen(false);
    setSnackbar(`Job "${name}" created`);
    loadJobs();
  };

  // Filtered and sorted jobs
  const filteredJobs = useMemo(() => {
    const q = jobSearch.toLowerCase();
    let list = jobs;
    if (q) {
      list = list.filter((j) => j.name.toLowerCase().includes(q));
    }
    if (jobFilter === 'enabled') {
      list = list.filter((j) => j.enabled);
    } else if (jobFilter === 'disabled') {
      list = list.filter((j) => !j.enabled);
    }
    const dir = jobSortDir === 'asc' ? 1 : -1;
    return [...list].sort((a, b) => compareJobs(a, b, jobSortKey) * dir);
  }, [jobs, jobSearch, jobFilter, jobSortKey, jobSortDir]);

  const hasMore = historyRuns.length < historyTotal;

  if (jobsLoading) return <LoadingSkeleton />;

  return (
    <Box>
      {/* Page header */}
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="h4">Cron Jobs</Typography>
        <Button
          variant="contained"
          startIcon={<AddIcon />}
          onClick={() => setNewJobOpen(true)}
        >
          New Job
        </Button>
      </Stack>

      {jobsError && (
        <Alert severity="warning" sx={{ mb: 2 }} onClose={() => setJobsError(null)}>
          {jobsError}
        </Alert>
      )}

      {/* Job list panel */}
      {jobs.length === 0 ? (
        <Box sx={{ textAlign: 'center', py: 6 }}>
          <Typography variant="body1" color="text.secondary" gutterBottom>
            No cron jobs yet. Create a task with a schedule to get started.
          </Typography>
          <Button
            variant="outlined"
            startIcon={<AddIcon />}
            onClick={() => setNewJobOpen(true)}
            sx={{ mt: 1 }}
          >
            Create Job
          </Button>
        </Box>
      ) : (
        <>
          {/* Job filters */}
          <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 2 }}>
            <TextField
              size="small"
              placeholder="Search jobs…"
              value={jobSearch}
              onChange={(e) => setJobSearch(e.target.value)}
              sx={{ maxWidth: 280 }}
            />
            <Select
              value={jobFilter}
              onChange={(e) => setJobFilter(e.target.value as 'all' | 'enabled' | 'disabled')}
              size="small"
              sx={{ minWidth: 120 }}
              startAdornment={<FilterListIcon sx={{ mr: 1, color: 'text.secondary' }} />}
            >
              <MenuItem value="all">All</MenuItem>
              <MenuItem value="enabled">Enabled</MenuItem>
              <MenuItem value="disabled">Disabled</MenuItem>
            </Select>
          </Stack>

          {/* Job table */}
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>
                    <TableSortLabel
                      active={jobSortKey === 'name'}
                      direction={jobSortKey === 'name' ? jobSortDir : 'asc'}
                      onClick={() => handleJobSort('name')}
                    >
                      Name
                    </TableSortLabel>
                  </TableCell>
                  <TableCell>Schedule</TableCell>
                  <TableCell>
                    <TableSortLabel
                      active={jobSortKey === 'next_run'}
                      direction={jobSortKey === 'next_run' ? jobSortDir : 'asc'}
                      onClick={() => handleJobSort('next_run')}
                    >
                      Next Run
                    </TableSortLabel>
                  </TableCell>
                  <TableCell>Last Run</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell align="center">Enabled</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {filteredJobs.map((job) => (
                  <TableRow
                    key={job.name}
                    hover
                    sx={{
                      cursor: 'pointer',
                      bgcolor: selectedJob === job.name ? 'action.selected' : undefined,
                    }}
                    onClick={() => handleJobClick(job.name)}
                  >
                    <TableCell>
                      <Typography variant="body2" fontWeight={500}>
                        {job.name}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2">
                        {job.schedule_human || job.schedule}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2" color="text.secondary">
                        {job.next_run ? relativeTime(job.next_run) : '-'}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2" color="text.secondary">
                        {relativeTime(job.last_run)}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      {job.last_status ? (
                        <Chip
                          label={job.last_status}
                          size="small"
                          color={statusColor(job.last_status)}
                          variant="outlined"
                        />
                      ) : (
                        <Typography variant="body2" color="text.secondary">-</Typography>
                      )}
                    </TableCell>
                    <TableCell align="center" onClick={(e) => e.stopPropagation()}>
                      <Switch
                        size="small"
                        checked={job.enabled}
                        disabled={toggling.has(job.name)}
                        onChange={() => handleToggle(job)}
                      />
                    </TableCell>
                  </TableRow>
                ))}
                {filteredJobs.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={6} align="center">
                      <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
                        No jobs match your search.
                      </Typography>
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </TableContainer>
        </>
      )}

      {/* Run history panel */}
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mt: 4, mb: 2 }}>
        <Stack direction="row" spacing={1} alignItems="center">
          <Typography variant="h6">Run History</Typography>
          {selectedJob && (
            <Chip
              label={selectedJob}
              size="small"
              onDelete={() => setSelectedJob(null)}
            />
          )}
        </Stack>
        <ToggleButtonGroup
          value={historyStatusFilter}
          exclusive
          onChange={(_, val) => { if (val !== null) setHistoryStatusFilter(val); }}
          size="small"
        >
          <ToggleButton value="all">All</ToggleButton>
          <ToggleButton value="success">Success</ToggleButton>
          <ToggleButton value="failed">Failed</ToggleButton>
          <ToggleButton value="timeout">Timeout</ToggleButton>
        </ToggleButtonGroup>
      </Stack>

      {historyError && (
        <Alert severity="warning" sx={{ mb: 2 }} onClose={() => setHistoryError(null)}>
          {historyError}
        </Alert>
      )}

      {historyLoading ? (
        <Box>
          <Skeleton width="100%" height={48} />
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} width="100%" height={52} />
          ))}
        </Box>
      ) : historyRuns.length === 0 ? (
        <Box sx={{ textAlign: 'center', py: 4 }}>
          <Typography variant="body2" color="text.secondary">
            {selectedJob
              ? `No run history for "${selectedJob}".`
              : 'No run history yet. Jobs will appear here after their first execution.'}
          </Typography>
        </Box>
      ) : (
        <>
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell width={40} />
                  <TableCell>Job Name</TableCell>
                  <TableCell>Started</TableCell>
                  <TableCell>Duration</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell>Summary</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {historyRuns.map((run) => (
                  <Box component="tbody" key={run.run_id}>
                    <TableRow
                      hover
                      sx={{ cursor: 'pointer' }}
                      onClick={() => handleExpandRun(run.run_id)}
                    >
                      <TableCell>
                        <IconButton size="small">
                          <ExpandMoreIcon
                            sx={{
                              transform: expandedRun === run.run_id ? 'rotate(180deg)' : 'none',
                              transition: 'transform 0.2s',
                            }}
                          />
                        </IconButton>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" fontWeight={500}>
                          {run.job_name}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" color="text.secondary">
                          {relativeTime(run.started_at)}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2">
                          {formatDuration(run.duration_ms)}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Chip
                          label={run.status}
                          size="small"
                          color={statusColor(run.status)}
                          variant="outlined"
                        />
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" color="text.secondary" noWrap sx={{ maxWidth: 300 }}>
                          {run.summary ? run.summary.slice(0, 80) + (run.summary.length > 80 ? '…' : '') : '-'}
                        </Typography>
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell colSpan={6} sx={{ py: 0, borderBottom: expandedRun === run.run_id ? undefined : 'none' }}>
                        <Collapse in={expandedRun === run.run_id} timeout="auto" unmountOnExit>
                          <HistoryDetail run={run} />
                        </Collapse>
                      </TableCell>
                    </TableRow>
                  </Box>
                ))}
              </TableBody>
            </Table>
          </TableContainer>

          {/* Load more */}
          {hasMore && (
            <Box sx={{ textAlign: 'center', mt: 2 }}>
              <Button
                variant="outlined"
                onClick={handleLoadMore}
                disabled={loadingMore}
              >
                {loadingMore ? 'Loading…' : `Load more (${historyRuns.length} of ${historyTotal})`}
              </Button>
            </Box>
          )}
        </>
      )}

      {/* New Job dialog */}
      <NewJobDialog
        open={newJobOpen}
        onClose={() => setNewJobOpen(false)}
        onCreated={handleJobCreated}
      />

      {/* Success snackbar */}
      <Snackbar
        open={!!snackbar}
        autoHideDuration={3000}
        onClose={() => setSnackbar(null)}
        message={snackbar}
      />
    </Box>
  );
}
