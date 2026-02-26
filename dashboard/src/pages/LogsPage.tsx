import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactElement } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Skeleton from '@mui/material/Skeleton';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import ToggleButton from '@mui/material/ToggleButton';
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup';
import Typography from '@mui/material/Typography';
import DownloadIcon from '@mui/icons-material/Download';
import PauseIcon from '@mui/icons-material/Pause';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import { List, useListRef } from 'react-window';
import { useThemeMode } from '../ThemeContext';
import { type LogEntry, fetchRecentLogs, createLogsWebSocket } from '../api/client';

const MAX_LINES = 1000;
const LEVELS = ['ALL', 'DEBUG', 'INFO', 'WARN', 'ERROR'] as const;
type LevelFilter = (typeof LEVELS)[number];

const LEVEL_COLORS: Record<string, 'info' | 'success' | 'warning' | 'error' | 'default'> = {
  DEBUG: 'info',
  INFO: 'success',
  WARNING: 'warning',
  WARN: 'warning',
  ERROR: 'error',
  CRITICAL: 'error',
};

const ROW_HEIGHT = 28;

function passesLevelFilter(entryLevel: string, filters: Set<LevelFilter>): boolean {
  if (filters.has('ALL') || filters.size === 0) return true;
  const upper = entryLevel.toUpperCase();
  if (upper === 'WARNING') return filters.has('WARN');
  return filters.has(upper as LevelFilter);
}

interface LogRowProps {
  entries: LogEntry[];
}

function LogRow(props: { index: number; style: CSSProperties; ariaAttributes: Record<string, unknown> } & LogRowProps): ReactElement | null {
  const { index, style, entries } = props;
  const entry = entries[index];
  if (!entry) return <div style={style} />;
  const levelColor = LEVEL_COLORS[entry.level.toUpperCase()] ?? 'default';
  return (
    <Box
      style={style}
      sx={{
        display: 'flex',
        alignItems: 'center',
        gap: 1,
        px: 1,
        fontFamily: 'monospace',
        fontSize: '0.8rem',
        lineHeight: `${ROW_HEIGHT}px`,
        '&:hover': {
          bgcolor: 'action.hover',
        },
      }}
    >
      <Typography
        component="span"
        variant="caption"
        sx={{ color: 'text.secondary', whiteSpace: 'nowrap', fontFamily: 'monospace', fontSize: '0.8rem' }}
      >
        {entry.timestamp}
      </Typography>
      <Chip
        label={entry.level}
        size="small"
        color={levelColor}
        variant="outlined"
        sx={{
          height: 18,
          fontSize: '0.65rem',
          minWidth: 48,
          '& .MuiChip-label': { px: 0.5 },
        }}
      />
      <Typography
        component="span"
        variant="caption"
        sx={{ color: 'text.secondary', whiteSpace: 'nowrap', fontFamily: 'monospace', fontSize: '0.8rem', minWidth: 100 }}
      >
        {entry.module}
      </Typography>
      <Typography
        component="span"
        variant="caption"
        sx={{
          fontFamily: 'monospace',
          fontSize: '0.8rem',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          flex: 1,
        }}
      >
        {entry.message}
      </Typography>
    </Box>
  );
}

export default function LogsPage() {
  const { mode } = useThemeMode();
  const [allLines, setAllLines] = useState<LogEntry[]>([]);
  const [filteredLines, setFilteredLines] = useState<LogEntry[]>([]);
  const [levelFilters, setLevelFilters] = useState<Set<LevelFilter>>(new Set(['ALL']));
  const [search, setSearch] = useState('');
  const [paused, setPaused] = useState(false);
  const [newLineCount, setNewLineCount] = useState(0);
  const [connected, setConnected] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [listHeight, setListHeight] = useState(400);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttempt = useRef(0);
  const listRef = useListRef(null);
  const pauseRef = useRef(paused);
  const containerRef = useRef<HTMLDivElement>(null);

  // Keep refs in sync
  pauseRef.current = paused;

  // Measure container height
  useEffect(() => {
    const measure = () => {
      if (containerRef.current) {
        setListHeight(containerRef.current.clientHeight);
      }
    };
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, []);

  // Apply filters to all lines
  const applyFilters = useCallback((lines: LogEntry[], levels: Set<LevelFilter>, searchText: string) => {
    const lowerSearch = searchText.toLowerCase();
    return lines.filter((entry) => {
      if (!passesLevelFilter(entry.level, levels)) return false;
      if (lowerSearch && !entry.message.toLowerCase().includes(lowerSearch)
        && !entry.module.toLowerCase().includes(lowerSearch)
        && !entry.timestamp.toLowerCase().includes(lowerSearch)) return false;
      return true;
    });
  }, []);

  // Re-filter when filters or search change
  useEffect(() => {
    setFilteredLines(applyFilters(allLines, levelFilters, search));
  }, [allLines, levelFilters, search, applyFilters]);

  // Auto-scroll to bottom when not paused
  useEffect(() => {
    if (!paused && listRef.current && filteredLines.length > 0) {
      try {
        listRef.current.scrollToRow({ index: filteredLines.length - 1, align: 'end' });
      } catch {
        // list may not be mounted yet
      }
    }
  }, [filteredLines, paused, listRef]);

  // REST fallback
  const fallbackToREST = useCallback(async () => {
    try {
      const data = await fetchRecentLogs({ limit: 200 });
      setAllLines(data.lines);
      setLoading(false);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to fetch logs');
      setLoading(false);
    }
  }, []);

  // WebSocket connection
  const connectWebSocket = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close();
    }

    try {
      const ws = createLogsWebSocket();
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        setReconnecting(false);
        setError(null);
        setLoading(false);
        reconnectAttempt.current = 0;
      };

      ws.onmessage = (event) => {
        try {
          const entry = JSON.parse(event.data) as LogEntry;
          setAllLines((prev) => {
            const next = [...prev, entry];
            if (next.length > MAX_LINES) {
              return next.slice(next.length - MAX_LINES);
            }
            return next;
          });
          if (pauseRef.current) {
            setNewLineCount((c) => c + 1);
          }
        } catch {
          // ignore malformed messages
        }
      };

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        // Auto-reconnect with exponential backoff
        const delay = Math.min(1000 * Math.pow(2, reconnectAttempt.current), 30000);
        reconnectAttempt.current += 1;
        setReconnecting(true);
        reconnectTimer.current = setTimeout(() => {
          connectWebSocket();
        }, delay);
      };

      ws.onerror = () => {
        // onclose will fire after onerror
      };
    } catch {
      setError('Failed to create WebSocket connection');
      setLoading(false);
      fallbackToREST();
    }
  }, [fallbackToREST]);

  // Initial connection
  useEffect(() => {
    connectWebSocket();

    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current);
      }
    };
  }, [connectWebSocket]);

  // Handle level filter change
  const handleLevelChange = (_: React.MouseEvent<HTMLElement>, newLevels: LevelFilter[]) => {
    if (newLevels.length === 0) {
      setLevelFilters(new Set(['ALL']));
      return;
    }
    const had = levelFilters.has('ALL');
    const nowHas = newLevels.includes('ALL');
    if (nowHas && !had) {
      setLevelFilters(new Set(['ALL']));
    } else if (nowHas && had && newLevels.length > 1) {
      setLevelFilters(new Set(newLevels.filter((l) => l !== 'ALL')));
    } else {
      setLevelFilters(new Set(newLevels));
    }
  };

  // Unpause and scroll to bottom
  const handleUnpause = () => {
    setPaused(false);
    setNewLineCount(0);
    if (listRef.current && filteredLines.length > 0) {
      listRef.current.scrollToRow({ index: filteredLines.length - 1, align: 'end' });
    }
  };

  // Download full log
  const handleDownload = async () => {
    try {
      const res = await fetch('/api/logs/recent?limit=1000');
      const data = await res.json();
      const lines = (data.lines as LogEntry[])
        .map((e) => `${e.timestamp} [${e.level}] ${e.module}: ${e.message}`)
        .join('\n');
      const blob = new Blob([lines], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'daemon.log';
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      // silently fail
    }
  };

  // Handle scroll to detect manual scroll up (pause)
  const handleScroll = useCallback((el: HTMLDivElement) => {
    const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < ROW_HEIGHT * 3;
    if (!isNearBottom && !pauseRef.current) {
      setPaused(true);
    }
  }, []);

  // Attach scroll listener to the outer list element
  useEffect(() => {
    const el = listRef.current?.element;
    if (!el) return;
    const handler = () => handleScroll(el);
    el.addEventListener('scroll', handler, { passive: true });
    return () => el.removeEventListener('scroll', handler);
  }, [listRef, handleScroll, filteredLines.length]);

  if (loading) {
    return (
      <Box>
        <Typography variant="h4" gutterBottom>Logs</Typography>
        <Stack spacing={1}>
          {Array.from({ length: 10 }).map((_, i) => (
            <Skeleton key={i} variant="text" height={ROW_HEIGHT} />
          ))}
        </Stack>
      </Box>
    );
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 100px)' }}>
      <Typography variant="h4" gutterBottom>Logs</Typography>

      {reconnecting && (
        <Alert severity="warning" sx={{ mb: 1 }}>
          WebSocket disconnected. Reconnecting...
        </Alert>
      )}

      {error && !reconnecting && (
        <Alert severity="error" sx={{ mb: 1 }}>
          {error}
        </Alert>
      )}

      {/* Controls */}
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1, flexWrap: 'wrap', gap: 1 }}>
        <ToggleButtonGroup
          size="small"
          value={Array.from(levelFilters)}
          onChange={handleLevelChange}
          aria-label="log level filter"
        >
          {LEVELS.map((level) => (
            <ToggleButton key={level} value={level} sx={{ textTransform: 'none', px: 1.5 }}>
              {level}
            </ToggleButton>
          ))}
        </ToggleButtonGroup>

        <TextField
          size="small"
          placeholder="Search logs..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          sx={{ minWidth: 200 }}
        />

        <Button
          size="small"
          variant={paused ? 'contained' : 'outlined'}
          startIcon={paused ? <PlayArrowIcon /> : <PauseIcon />}
          onClick={paused ? handleUnpause : () => setPaused(true)}
        >
          {paused ? 'Resume' : 'Pause'}
        </Button>

        {paused && newLineCount > 0 && (
          <Chip
            label={`${newLineCount} new line${newLineCount !== 1 ? 's' : ''}`}
            color="primary"
            size="small"
            onClick={handleUnpause}
            sx={{ cursor: 'pointer' }}
          />
        )}

        <Button
          size="small"
          variant="outlined"
          startIcon={<DownloadIcon />}
          onClick={handleDownload}
        >
          Download
        </Button>

        <Chip
          label={connected ? 'Live' : 'Disconnected'}
          color={connected ? 'success' : 'default'}
          size="small"
          variant="outlined"
        />
      </Stack>

      {/* Log lines */}
      <Box
        ref={containerRef}
        sx={{
          flex: 1,
          border: 1,
          borderColor: 'divider',
          borderRadius: 1,
          bgcolor: mode === 'dark' ? 'grey.900' : 'grey.50',
          overflow: 'hidden',
        }}
      >
        {filteredLines.length === 0 ? (
          <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
            <Typography color="text.secondary">
              {allLines.length === 0 ? 'No log lines yet. Waiting for daemon activity...' : 'No lines match current filters.'}
            </Typography>
          </Box>
        ) : (
          <List<LogRowProps>
            listRef={listRef}
            style={{ height: listHeight || 400 }}
            rowComponent={LogRow}
            rowCount={filteredLines.length}
            rowHeight={ROW_HEIGHT}
            rowProps={{ entries: filteredLines }}
            overscanCount={20}
          />
        )}
      </Box>
    </Box>
  );
}
