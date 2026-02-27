import { useCallback, useEffect, useRef, useState } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import Grid from '@mui/material/Grid';
import Skeleton from '@mui/material/Skeleton';
import Stack from '@mui/material/Stack';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableContainer from '@mui/material/TableContainer';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';
import Typography from '@mui/material/Typography';
import type { StatusResponse } from '../api/client';
import { fetchStatus } from '../api/client';

const POLL_INTERVAL_MS = 30_000;

// ---- helpers ----

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ${Math.round(seconds % 60)}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

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
  return `${(ms / 1000).toFixed(1)}s`;
}

function statusColor(status: string): 'success' | 'error' | 'warning' | 'default' {
  switch (status) {
    case 'success': return 'success';
    case 'failed': return 'error';
    case 'timeout': return 'warning';
    default: return 'default';
  }
}

// ---- skeleton cards while loading ----

function LoadingSkeleton() {
  return (
    <Grid container spacing={2}>
      {[1, 2, 3].map((i) => (
        <Grid key={i} size={{ xs: 12, md: 4 }}>
          <Card>
            <CardContent>
              <Skeleton width="40%" height={28} />
              <Skeleton width="80%" sx={{ mt: 1 }} />
              <Skeleton width="60%" />
            </CardContent>
          </Card>
        </Grid>
      ))}
      <Grid size={12}>
        <Card>
          <CardContent>
            <Skeleton width="30%" height={28} />
            <Skeleton width="100%" height={40} sx={{ mt: 1 }} />
            <Skeleton width="100%" height={40} />
            <Skeleton width="100%" height={40} />
          </CardContent>
        </Card>
      </Grid>
    </Grid>
  );
}

// ---- main component ----

export default function OverviewPage() {
  const [data, setData] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const status = await fetchStatus();
      setData(status);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch status');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    timerRef.current = setInterval(load, POLL_INTERVAL_MS);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [load]);

  if (loading) return <LoadingSkeleton />;

  if (error && !data) {
    return (
      <Alert severity="error" sx={{ mt: 2 }}>
        Daemon unreachable: {error}
      </Alert>
    );
  }

  // data guaranteed non-null past this point (or we showed error)
  const status = data!;

  return (
    <Box>
      <Typography variant="h4" gutterBottom>
        Overview
      </Typography>

      {error && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          Refresh failed: {error}
        </Alert>
      )}

      <Grid container spacing={2}>
        {/* Daemon status card */}
        <Grid size={{ xs: 12, md: 4 }}>
          <Card>
            <CardContent>
              <Typography variant="subtitle2" color="text.secondary" gutterBottom>
                Daemon
              </Typography>
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
                <Chip
                  size="small"
                  label={status.daemon.running ? 'Running' : 'Stopped'}
                  color={status.daemon.running ? 'success' : 'error'}
                />
              </Stack>
              <Typography variant="body2">PID: {status.daemon.pid}</Typography>
              <Typography variant="body2">
                Uptime: {formatUptime(status.daemon.uptime_seconds)}
              </Typography>
              <Typography variant="body2" noWrap title={status.daemon.socket}>
                Socket: {status.daemon.socket}
              </Typography>
            </CardContent>
          </Card>
        </Grid>

        {/* Agent info card */}
        <Grid size={{ xs: 12, md: 4 }}>
          <Card>
            <CardContent>
              <Typography variant="subtitle2" color="text.secondary" gutterBottom>
                Agent
              </Typography>
              <Typography variant="body2">Name: {status.agent.name}</Typography>
              <Typography variant="body2">Model: {status.agent.model}</Typography>
              <Typography variant="body2">Provider: {status.agent.provider}</Typography>
            </CardContent>
          </Card>
        </Grid>

        {/* Quick stats card */}
        <Grid size={{ xs: 12, md: 4 }}>
          <Card>
            <CardContent>
              <Typography variant="subtitle2" color="text.secondary" gutterBottom>
                Quick Stats
              </Typography>
              <Typography variant="body2">
                Total tasks: {status.tasks.total}
              </Typography>
              <Typography variant="body2">
                Scheduled: {status.tasks.scheduled}
              </Typography>
              <Typography variant="body2">
                Cron jobs: {status.cron.enabled_jobs}
              </Typography>
              <Typography variant="body2">
                Next run: {status.cron.next_run ?? 'None'}
              </Typography>
            </CardContent>
          </Card>
        </Grid>

        {/* Channels section */}
        <Grid size={12}>
          <Card>
            <CardContent>
              <Typography variant="subtitle2" color="text.secondary" gutterBottom>
                Channels
              </Typography>
              {status.channels.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  No channels configured
                </Typography>
              ) : (
                <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                  {status.channels.map((ch) => (
                    <Chip
                      key={ch.name}
                      label={ch.name}
                      size="small"
                      color={ch.connected ? 'success' : 'error'}
                      variant="outlined"
                    />
                  ))}
                </Stack>
              )}
            </CardContent>
          </Card>
        </Grid>

        {/* Recent runs */}
        <Grid size={12}>
          <Card>
            <CardContent>
              <Typography variant="subtitle2" color="text.secondary" gutterBottom>
                Recent Runs
              </Typography>
              {status.recent_runs.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  No recent runs
                </Typography>
              ) : (
                <TableContainer>
                  <Table size="small">
                    <TableHead>
                      <TableRow>
                        <TableCell>Task</TableCell>
                        <TableCell>Status</TableCell>
                        <TableCell>Finished</TableCell>
                        <TableCell>Duration</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {status.recent_runs.map((run, i) => (
                        <TableRow key={i}>
                          <TableCell>{run.task_name}</TableCell>
                          <TableCell>
                            <Chip
                              size="small"
                              label={run.status}
                              color={statusColor(run.status)}
                            />
                          </TableCell>
                          <TableCell>{relativeTime(run.finished_at)}</TableCell>
                          <TableCell>{formatDuration(run.duration_ms)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
              )}
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </Box>
  );
}
