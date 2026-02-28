import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import AddIcon from '@mui/icons-material/Add';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Skeleton from '@mui/material/Skeleton';
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
import Typography from '@mui/material/Typography';
import type { TaskSummary } from '../api/client';
import { fetchTasks, toggleTaskEnabled } from '../api/client';

type SortKey = 'name' | 'schedule' | 'last_modified';
type SortDir = 'asc' | 'desc';

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

function humanSchedule(cron: string): string {
  if (!cron) return '-';
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return cron;
  const [min, hour, dom, mon, dow] = parts;
  if (min === '*' && hour === '*') return 'Every minute';
  if (hour === '*') return `Every hour at :${min.padStart(2, '0')}`;
  if (dom === '*' && mon === '*' && dow === '*') return `Daily at ${hour}:${min.padStart(2, '0')}`;
  if (dow !== '*' && dom === '*' && mon === '*') return `Weekly (${dow}) at ${hour}:${min.padStart(2, '0')}`;
  return cron;
}

function compare(a: TaskSummary, b: TaskSummary, key: SortKey): number {
  switch (key) {
    case 'name':
      return a.name.localeCompare(b.name);
    case 'schedule':
      return a.schedule.localeCompare(b.schedule);
    case 'last_modified':
      return (a.last_modified ?? '').localeCompare(b.last_modified ?? '');
  }
}

// ---- Loading skeleton ----

function LoadingSkeleton() {
  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }}>
        <Skeleton width={80} height={36} />
        <Skeleton width={120} height={36} />
      </Stack>
      <Skeleton width="100%" height={48} />
      {[1, 2, 3, 4].map((i) => (
        <Skeleton key={i} width="100%" height={52} />
      ))}
    </Box>
  );
}

// ---- Main component ----

export default function TaskListPage() {
  const navigate = useNavigate();
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('name');
  const [sortDir, setSortDir] = useState<SortDir>('asc');
  const [toggling, setToggling] = useState<Set<string>>(new Set());
  const mountedRef = useRef(true);

  const load = useCallback(async () => {
    try {
      const list = await fetchTasks();
      if (mountedRef.current) {
        setTasks(list);
        setError(null);
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : 'Failed to fetch tasks');
      }
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    load();
    return () => { mountedRef.current = false; };
  }, [load]);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  };

  const handleToggle = async (task: TaskSummary) => {
    setToggling((prev) => new Set(prev).add(task.name));
    try {
      await toggleTaskEnabled(task.name, !task.enabled);
      setTasks((prev) =>
        prev.map((t) => (t.name === task.name ? { ...t, enabled: !t.enabled } : t)),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to toggle task');
    } finally {
      setToggling((prev) => {
        const next = new Set(prev);
        next.delete(task.name);
        return next;
      });
    }
  };

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    let list = tasks;
    if (q) {
      list = list.filter(
        (t) => t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q),
      );
    }
    const dir = sortDir === 'asc' ? 1 : -1;
    return [...list].sort((a, b) => compare(a, b, sortKey) * dir);
  }, [tasks, search, sortKey, sortDir]);

  if (loading) return <LoadingSkeleton />;

  if (error && tasks.length === 0) {
    return (
      <Alert severity="error" sx={{ mt: 2 }}>
        {error}
      </Alert>
    );
  }

  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="h4">Tasks</Typography>
        <Button
          variant="contained"
          startIcon={<AddIcon />}
          onClick={() => navigate('/tasks/new')}
        >
          New Task
        </Button>
      </Stack>

      {error && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {tasks.length === 0 ? (
        <Box sx={{ textAlign: 'center', py: 6 }}>
          <Typography variant="body1" color="text.secondary" gutterBottom>
            No tasks yet. Create your first task.
          </Typography>
          <Button
            variant="outlined"
            startIcon={<AddIcon />}
            onClick={() => navigate('/tasks/new')}
            sx={{ mt: 1 }}
          >
            Create Task
          </Button>
        </Box>
      ) : (
        <>
          <TextField
            size="small"
            placeholder="Search tasks…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            sx={{ mb: 2, maxWidth: 360 }}
            fullWidth
          />

          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>
                    <TableSortLabel
                      active={sortKey === 'name'}
                      direction={sortKey === 'name' ? sortDir : 'asc'}
                      onClick={() => handleSort('name')}
                    >
                      Name
                    </TableSortLabel>
                  </TableCell>
                  <TableCell>Description</TableCell>
                  <TableCell>
                    <TableSortLabel
                      active={sortKey === 'schedule'}
                      direction={sortKey === 'schedule' ? sortDir : 'asc'}
                      onClick={() => handleSort('schedule')}
                    >
                      Schedule
                    </TableSortLabel>
                  </TableCell>
                  <TableCell>
                    <TableSortLabel
                      active={sortKey === 'last_modified'}
                      direction={sortKey === 'last_modified' ? sortDir : 'asc'}
                      onClick={() => handleSort('last_modified')}
                    >
                      Last Modified
                    </TableSortLabel>
                  </TableCell>
                  <TableCell align="center">Enabled</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {filtered.map((task) => (
                  <TableRow
                    key={task.name}
                    hover
                    sx={{ cursor: 'pointer' }}
                    onClick={() => navigate(`/tasks/${encodeURIComponent(task.name)}`)}
                  >
                    <TableCell>
                      <Typography variant="body2" fontWeight={500}>
                        {task.name}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2" color="text.secondary" noWrap sx={{ maxWidth: 300 }}>
                        {task.description || '-'}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2">
                        {humanSchedule(task.schedule)}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2">
                        {relativeTime(task.last_modified)}
                      </Typography>
                    </TableCell>
                    <TableCell align="center" onClick={(e) => e.stopPropagation()}>
                      <Switch
                        size="small"
                        checked={task.enabled}
                        disabled={toggling.has(task.name)}
                        onChange={() => handleToggle(task)}
                      />
                    </TableCell>
                  </TableRow>
                ))}
                {filtered.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={5} align="center">
                      <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
                        No tasks match your search.
                      </Typography>
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </TableContainer>
        </>
      )}
    </Box>
  );
}
