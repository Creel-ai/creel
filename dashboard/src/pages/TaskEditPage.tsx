import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import yaml from 'js-yaml';

import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Checkbox from '@mui/material/Checkbox';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogContentText from '@mui/material/DialogContentText';
import DialogTitle from '@mui/material/DialogTitle';
import FormControlLabel from '@mui/material/FormControlLabel';
import MenuItem from '@mui/material/MenuItem';
import Select from '@mui/material/Select';
import Skeleton from '@mui/material/Skeleton';
import Snackbar from '@mui/material/Snackbar';
import Stack from '@mui/material/Stack';
import Tab from '@mui/material/Tab';
import Tabs from '@mui/material/Tabs';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';

import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import DeleteIcon from '@mui/icons-material/Delete';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import SaveIcon from '@mui/icons-material/Save';

import type { TaskDetail, TaskUpdateRequest } from '../api/client';
import {
  createTask,
  deleteTask,
  fetchTaskDetail,
  runTask,
  updateTask,
} from '../api/client';

import { EditorView, basicSetup } from 'codemirror';
import { EditorState } from '@codemirror/state';
import { yaml as yamlMode } from '@codemirror/lang-yaml';
import { oneDark } from '@codemirror/theme-one-dark';
import { useThemeMode } from '../ThemeContext';

// ---- Schedule presets ----

const SCHEDULE_PRESETS = [
  { label: 'Custom', value: '' },
  { label: 'Every minute', value: '* * * * *' },
  { label: 'Hourly', value: '0 * * * *' },
  { label: 'Daily at 9:00 AM', value: '0 9 * * *' },
  { label: 'Daily at midnight', value: '0 0 * * *' },
  { label: 'Weekly (Monday 9 AM)', value: '0 9 * * 1' },
  { label: 'Weekly (Sunday midnight)', value: '0 0 * * 0' },
];

function humanSchedule(cron: string): string {
  if (!cron) return '';
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return cron;
  const [min, hour, dom, mon, dow] = parts;
  if (min === '*' && hour === '*') return 'Every minute';
  if (hour === '*') return `Every hour at :${min.padStart(2, '0')}`;
  if (dom === '*' && mon === '*' && dow === '*')
    return `Daily at ${hour}:${min.padStart(2, '0')}`;
  if (dow !== '*' && dom === '*' && mon === '*') {
    const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const dayName = days[Number(dow)] ?? dow;
    return `Weekly (${dayName}) at ${hour}:${min.padStart(2, '0')}`;
  }
  return cron;
}

const SLUG_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;

// ---- Form state ----

interface FormFields {
  name: string;
  description: string;
  schedule: string;
  prompt: string;
  model: string;
  max_tokens: number;
  timeout: number;
  output_type: string;
  output_to: string;
  enabled: boolean;
  mode: string;
}

function emptyForm(): FormFields {
  return {
    name: '',
    description: '',
    schedule: '0 9 * * *',
    prompt: '',
    model: '',
    max_tokens: 300,
    timeout: 0,
    output_type: 'stdout',
    output_to: '',
    enabled: true,
    mode: 'simple',
  };
}

function formToYaml(f: FormFields): string {
  const doc: Record<string, unknown> = {
    name: f.name,
    schedule: f.schedule,
    prompt: f.prompt,
    output: { type: f.output_type, to: f.output_to },
    llm: { model: f.model || undefined, max_tokens: f.max_tokens },
    mode: f.mode,
  };
  if (f.description) doc.description = f.description;
  if (!f.enabled) doc.enabled = false;
  if (f.timeout > 0) doc.timeout = f.timeout;
  return yaml.dump(doc, { lineWidth: -1, sortKeys: false });
}

function yamlToForm(raw: string): FormFields | null {
  try {
    const parsed = yaml.load(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    const d = parsed as Record<string, unknown>;
    const output =
      d.output && typeof d.output === 'object'
        ? (d.output as Record<string, unknown>)
        : {};
    const llm =
      d.llm && typeof d.llm === 'object'
        ? (d.llm as Record<string, unknown>)
        : {};
    return {
      name: String(d.name ?? ''),
      description: String(d.description ?? ''),
      schedule: String(d.schedule ?? ''),
      prompt: String(d.prompt ?? ''),
      model: String(llm.model ?? ''),
      max_tokens: Number(llm.max_tokens ?? 300),
      timeout: Number(d.timeout ?? 0),
      output_type: String(output.type ?? 'stdout'),
      output_to: String(output.to ?? ''),
      enabled: d.enabled !== false,
      mode: String(d.mode ?? 'simple'),
    };
  } catch {
    return null;
  }
}

// ---- CodeMirror wrapper ----

function YamlEditor({
  value,
  onChange,
  darkMode,
}: {
  value: string;
  onChange: (val: string) => void;
  darkMode: boolean;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  // Track whether the value change came from the editor itself
  const internalUpdate = useRef(false);

  useEffect(() => {
    if (!containerRef.current) return;
    const extensions = [
      basicSetup,
      yamlMode(),
      EditorView.updateListener.of((update) => {
        if (update.docChanged) {
          internalUpdate.current = true;
          onChangeRef.current(update.state.doc.toString());
        }
      }),
      EditorView.lineWrapping,
    ];
    if (darkMode) extensions.push(oneDark);

    const state = EditorState.create({ doc: value, extensions });
    const view = new EditorView({ state, parent: containerRef.current });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // Recreate editor when dark mode changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [darkMode]);

  // Sync external value changes into the editor
  useEffect(() => {
    if (internalUpdate.current) {
      internalUpdate.current = false;
      return;
    }
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (current !== value) {
      view.dispatch({
        changes: { from: 0, to: current.length, insert: value },
      });
    }
  }, [value]);

  return (
    <Box
      ref={containerRef}
      sx={{
        border: 1,
        borderColor: 'divider',
        borderRadius: 1,
        overflow: 'auto',
        minHeight: 300,
        '& .cm-editor': { height: '100%', minHeight: 300 },
        '& .cm-scroller': { overflow: 'auto' },
      }}
    />
  );
}

// ---- Main component ----

export default function TaskEditPage() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const { mode: themeMode } = useThemeMode();
  const isEdit = !!name;

  const [loading, setLoading] = useState(isEdit);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState(0); // 0=Form, 1=Raw
  const [form, setForm] = useState<FormFields>(emptyForm());
  const [rawYaml, setRawYaml] = useState('');
  const [savedYaml, setSavedYaml] = useState(''); // for dirty tracking
  const [saving, setSaving] = useState(false);
  const [snackbar, setSnackbar] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [nameError, setNameError] = useState('');
  const [promptError, setPromptError] = useState('');

  const mountedRef = useRef(true);

  // Load task for edit mode
  const loadTask = useCallback(async () => {
    if (!name) return;
    try {
      const detail: TaskDetail = await fetchTaskDetail(name);
      if (!mountedRef.current) return;
      setRawYaml(detail.raw_yaml);
      setSavedYaml(detail.raw_yaml);
      const parsed = yamlToForm(detail.raw_yaml);
      if (parsed) setForm(parsed);
      else {
        setForm({
          ...emptyForm(),
          name: detail.name,
          description: detail.description,
          schedule: detail.schedule,
          prompt: detail.prompt,
          model: detail.model,
          max_tokens: detail.max_tokens,
          output_type: detail.output_type,
          output_to: detail.output_to,
          enabled: detail.enabled,
          mode: detail.mode,
        });
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : 'Failed to load task');
      }
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [name]);

  useEffect(() => {
    mountedRef.current = true;
    loadTask();
    return () => {
      mountedRef.current = false;
    };
  }, [loadTask]);

  // Dirty tracking
  const isDirty = isEdit ? rawYaml !== savedYaml : form.name !== '' || form.prompt !== '';

  // Unsaved changes warning
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (isDirty) e.preventDefault();
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [isDirty]);

  // Tab switching: sync form <-> raw
  const handleTabChange = (_: React.SyntheticEvent, newTab: number) => {
    if (newTab === 1 && tab === 0) {
      // Form -> Raw: serialize form to YAML
      setRawYaml(formToYaml(form));
    } else if (newTab === 0 && tab === 1) {
      // Raw -> Form: parse YAML to form
      const parsed = yamlToForm(rawYaml);
      if (parsed) {
        setForm(parsed);
      }
      // If parse fails, keep old form — user will see stale data
    }
    setTab(newTab);
  };

  // Form field updaters
  const updateField = <K extends keyof FormFields>(key: K, value: FormFields[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  // Validation
  const validateForm = (): boolean => {
    let valid = true;
    if (!isEdit && !SLUG_PATTERN.test(form.name)) {
      setNameError('Must start with a letter or digit. Only lowercase letters, digits, hyphens, underscores.');
      valid = false;
    } else {
      setNameError('');
    }
    if (!form.prompt.trim()) {
      setPromptError('Prompt is required');
      valid = false;
    } else {
      setPromptError('');
    }
    return valid;
  };

  // Save
  const handleSave = async () => {
    if (tab === 0) {
      if (!validateForm()) return;
    }

    setSaving(true);
    setError(null);
    try {
      const yamlStr = tab === 0 ? formToYaml(form) : rawYaml;
      const data: TaskUpdateRequest = {
        name: isEdit ? name! : form.name,
        raw_yaml: yamlStr,
      };

      if (isEdit) {
        await updateTask(name!, data);
      } else {
        await createTask(data);
      }

      setSavedYaml(yamlStr);
      setRawYaml(yamlStr);
      setSnackbar(isEdit ? 'Task saved' : 'Task created');

      if (!isEdit) {
        navigate(`/tasks/${encodeURIComponent(form.name)}`, { replace: true });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  // Run
  const handleRun = async () => {
    if (!name) return;
    try {
      const result = await runTask(name);
      setSnackbar(`Task run started (${result.run_id.slice(0, 8)}...)`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Run failed');
    }
  };

  // Delete
  const handleDelete = async () => {
    if (!name) return;
    setDeleting(true);
    try {
      await deleteTask(name);
      setSnackbar('Task deleted');
      navigate('/tasks', { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed');
      setDeleting(false);
      setDeleteOpen(false);
    }
  };

  // Schedule preset
  const schedulePreset = SCHEDULE_PRESETS.find((p) => p.value === form.schedule)?.value ?? '';

  if (loading) {
    return (
      <Box>
        <Skeleton width={200} height={40} />
        <Skeleton width="100%" height={48} sx={{ mt: 2 }} />
        <Skeleton width="100%" height={300} sx={{ mt: 1 }} />
      </Box>
    );
  }

  return (
    <Box>
      {/* Header */}
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 2 }}>
        <Stack direction="row" alignItems="center" spacing={1}>
          <Button
            size="small"
            startIcon={<ArrowBackIcon />}
            onClick={() => navigate('/tasks')}
          >
            Tasks
          </Button>
          <Typography variant="h5">
            {isEdit ? `Edit: ${name}` : 'New Task'}
          </Typography>
        </Stack>
        <Stack direction="row" spacing={1}>
          {isEdit && (
            <>
              <Button
                variant="outlined"
                startIcon={<PlayArrowIcon />}
                onClick={handleRun}
              >
                Run Now
              </Button>
              <Button
                variant="outlined"
                color="error"
                startIcon={<DeleteIcon />}
                onClick={() => setDeleteOpen(true)}
              >
                Delete
              </Button>
            </>
          )}
          <Button
            variant="contained"
            startIcon={<SaveIcon />}
            onClick={handleSave}
            disabled={saving || !isDirty}
          >
            {saving ? 'Saving…' : 'Save'}
          </Button>
        </Stack>
      </Stack>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Tabs */}
      <Tabs value={tab} onChange={handleTabChange} sx={{ mb: 2 }}>
        <Tab label="Form" />
        <Tab label="Raw YAML" />
      </Tabs>

      {/* Form tab */}
      {tab === 0 && (
        <Stack spacing={2} sx={{ maxWidth: 640 }}>
          <TextField
            label="Name"
            value={form.name}
            onChange={(e) => updateField('name', e.target.value)}
            disabled={isEdit}
            required
            error={!!nameError}
            helperText={nameError || 'Lowercase letters, digits, hyphens, underscores (e.g. daily-report)'}
            size="small"
            fullWidth
          />
          <TextField
            label="Description"
            value={form.description}
            onChange={(e) => updateField('description', e.target.value)}
            multiline
            rows={2}
            size="small"
            fullWidth
          />
          <Stack direction="row" spacing={2} alignItems="flex-start">
            <TextField
              label="Schedule (cron)"
              value={form.schedule}
              onChange={(e) => updateField('schedule', e.target.value)}
              size="small"
              fullWidth
              helperText={humanSchedule(form.schedule)}
            />
            <Select
              value={schedulePreset}
              onChange={(e) => {
                if (e.target.value) updateField('schedule', e.target.value);
              }}
              size="small"
              displayEmpty
              sx={{ minWidth: 180 }}
            >
              {SCHEDULE_PRESETS.map((p) => (
                <MenuItem key={p.value} value={p.value}>
                  {p.label}
                </MenuItem>
              ))}
            </Select>
          </Stack>
          <TextField
            label="Prompt"
            value={form.prompt}
            onChange={(e) => updateField('prompt', e.target.value)}
            required
            error={!!promptError}
            helperText={promptError}
            multiline
            rows={6}
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
          <Stack direction="row" spacing={2}>
            <TextField
              label="Max tokens"
              value={form.max_tokens}
              onChange={(e) => updateField('max_tokens', Number(e.target.value) || 0)}
              type="number"
              size="small"
              sx={{ width: 160 }}
            />
            <TextField
              label="Timeout (seconds)"
              value={form.timeout || ''}
              onChange={(e) => updateField('timeout', Number(e.target.value) || 0)}
              type="number"
              size="small"
              sx={{ width: 160 }}
              helperText="0 = no timeout"
            />
          </Stack>
          <Stack direction="row" spacing={2} alignItems="flex-start">
            <Select
              value={form.output_type}
              onChange={(e) => updateField('output_type', e.target.value)}
              size="small"
              sx={{ minWidth: 160 }}
            >
              <MenuItem value="stdout">stdout</MenuItem>
              <MenuItem value="imessage">iMessage</MenuItem>
              <MenuItem value="file">File</MenuItem>
            </Select>
            <TextField
              label="Output target"
              value={form.output_to}
              onChange={(e) => updateField('output_to', e.target.value)}
              size="small"
              fullWidth
              helperText={
                form.output_type === 'imessage'
                  ? 'Phone number or email'
                  : form.output_type === 'file'
                    ? 'File path'
                    : 'Optional'
              }
            />
          </Stack>
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
      )}

      {/* Raw YAML tab */}
      {tab === 1 && (
        <YamlEditor
          value={rawYaml}
          onChange={setRawYaml}
          darkMode={themeMode === 'dark'}
        />
      )}

      {/* Delete confirmation dialog */}
      <Dialog open={deleteOpen} onClose={() => setDeleteOpen(false)}>
        <DialogTitle>Delete Task</DialogTitle>
        <DialogContent>
          <DialogContentText>
            Are you sure you want to delete &quot;{name}&quot;? The task file will be moved to
            a .deleted directory and can be recovered manually.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDeleteOpen(false)} disabled={deleting}>
            Cancel
          </Button>
          <Button
            onClick={handleDelete}
            color="error"
            variant="contained"
            disabled={deleting}
          >
            {deleting ? 'Deleting…' : 'Delete'}
          </Button>
        </DialogActions>
      </Dialog>

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
