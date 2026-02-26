import { useCallback, useEffect, useRef, useState } from 'react';
import yaml from 'js-yaml';

import Accordion from '@mui/material/Accordion';
import AccordionDetails from '@mui/material/AccordionDetails';
import AccordionSummary from '@mui/material/AccordionSummary';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogContentText from '@mui/material/DialogContentText';
import DialogTitle from '@mui/material/DialogTitle';
import FormControlLabel from '@mui/material/FormControlLabel';
import FormHelperText from '@mui/material/FormHelperText';
import MenuItem from '@mui/material/MenuItem';
import Select from '@mui/material/Select';
import Skeleton from '@mui/material/Skeleton';
import Snackbar from '@mui/material/Snackbar';
import Stack from '@mui/material/Stack';
import Switch from '@mui/material/Switch';
import Tab from '@mui/material/Tab';
import Tabs from '@mui/material/Tabs';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';

import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import RestartAltIcon from '@mui/icons-material/RestartAlt';
import SaveIcon from '@mui/icons-material/Save';

import {
  applyConfig,
  fetchConfig,
  fetchConfigSchema,
  updateConfig,
} from '../api/client';

import { EditorView, basicSetup } from 'codemirror';
import { EditorState } from '@codemirror/state';
import { yaml as yamlMode } from '@codemirror/lang-yaml';
import { oneDark } from '@codemirror/theme-one-dark';
import { useThemeMode } from '../ThemeContext';

// ---- Types for form fields ----

interface ConfigForm {
  // Agent section
  system_prompt: string;
  system_prompt_file: string;
  // LLM section
  llm_model: string;
  llm_max_tokens: number;
  llm_secrets: string;
  // Agent loop section
  agent_max_turns: number;
  // Session section
  session_sessions_dir: string;
  session_max_history: number;
  session_summarize_on_trim: boolean;
  session_ttl_hours: number;
  session_summary_model: string;
  session_summary_max_tokens: number;
  session_max_context_tokens: number;
  session_encryption_key: string;
  // Workspace section
  workspace_path: string;
  workspace_timezone: string;
  workspace_memory_days: number;
  workspace_memory_max_chars: number;
  workspace_max_chars_per_file: number;
  workspace_compact_after_days: number;
  workspace_max_daily_entries: number;
  workspace_max_long_term_lines: number;
  // Quiet hours section
  quiet_hours_enabled: boolean;
  quiet_hours_start: string;
  quiet_hours_end: string;
  quiet_hours_timezone: string;
  quiet_hours_allow_urgent: boolean;
  // Bridge section
  bridge_enabled: boolean;
  bridge_url: string;
  bridge_token: string;
  // Browser section
  browser_enabled: boolean;
  browser_default_mode: string;
  browser_cdp_url: string;
  browser_max_sessions: number;
  browser_session_timeout_minutes: number;
  browser_headless: boolean;
  browser_container_memory: string;
  browser_container_shm_size: string;
  browser_container_tmpfs_size: string;
  browser_navigate_timeout_ms: number;
  browser_snapshot_timeout_ms: number;
  browser_block_heavy_resources: boolean;
  // Guardian section
  guardian_enabled: boolean;
  guardian_debug: boolean;
  guardian_fc_enabled: boolean;
  guardian_fc_threshold: number;
  guardian_lj_enabled: boolean;
  guardian_lj_model: string;
  guardian_policy_enabled: boolean;
  guardian_policy_file: string;
  guardian_audit_enabled: boolean;
  guardian_audit_log_file: string;
  // Daemon section (from log_level in config if present)
  daemon_log_level: string;
  daemon_port: number;
}

function defaultForm(): ConfigForm {
  return {
    system_prompt: '',
    system_prompt_file: '',
    llm_model: 'claude-sonnet-4-20250514',
    llm_max_tokens: 300,
    llm_secrets: '',
    agent_max_turns: 10,
    session_sessions_dir: 'sessions',
    session_max_history: 50,
    session_summarize_on_trim: true,
    session_ttl_hours: 0,
    session_summary_model: 'claude-haiku-4-5-20251001',
    session_summary_max_tokens: 1024,
    session_max_context_tokens: 180000,
    session_encryption_key: '',
    workspace_path: 'workspace',
    workspace_timezone: 'UTC',
    workspace_memory_days: 2,
    workspace_memory_max_chars: 20000,
    workspace_max_chars_per_file: 20000,
    workspace_compact_after_days: 7,
    workspace_max_daily_entries: 50,
    workspace_max_long_term_lines: 500,
    quiet_hours_enabled: false,
    quiet_hours_start: '23:00',
    quiet_hours_end: '08:00',
    quiet_hours_timezone: 'UTC',
    quiet_hours_allow_urgent: true,
    bridge_enabled: false,
    bridge_url: 'http://localhost:8766',
    bridge_token: '',
    browser_enabled: false,
    browser_default_mode: 'managed',
    browser_cdp_url: '',
    browser_max_sessions: 3,
    browser_session_timeout_minutes: 10,
    browser_headless: true,
    browser_container_memory: '1024m',
    browser_container_shm_size: '256m',
    browser_container_tmpfs_size: '128M',
    browser_navigate_timeout_ms: 30000,
    browser_snapshot_timeout_ms: 15000,
    browser_block_heavy_resources: true,
    guardian_enabled: true,
    guardian_debug: false,
    guardian_fc_enabled: true,
    guardian_fc_threshold: 0.85,
    guardian_lj_enabled: true,
    guardian_lj_model: 'claude-haiku-4-5-20251001',
    guardian_policy_enabled: true,
    guardian_policy_file: 'policies/default.yaml',
    guardian_audit_enabled: true,
    guardian_audit_log_file: 'guardian_audit.jsonl',
    daemon_log_level: 'INFO',
    daemon_port: 8099,
  };
}

function configToForm(config: Record<string, unknown>): ConfigForm {
  const f = defaultForm();
  const get = (obj: unknown, key: string): unknown => {
    if (obj && typeof obj === 'object' && key in (obj as Record<string, unknown>))
      return (obj as Record<string, unknown>)[key];
    return undefined;
  };

  f.system_prompt = String(config.system_prompt ?? f.system_prompt);
  f.system_prompt_file = String(config.system_prompt_file ?? '');

  const llm = config.llm as Record<string, unknown> | undefined;
  if (llm) {
    f.llm_model = String(llm.model ?? f.llm_model);
    f.llm_max_tokens = Number(llm.max_tokens ?? f.llm_max_tokens);
    f.llm_secrets = String(llm.secrets ?? '');
  }

  const agent = config.agent as Record<string, unknown> | undefined;
  if (agent) {
    f.agent_max_turns = Number(agent.max_turns ?? f.agent_max_turns);
  }

  const session = config.session as Record<string, unknown> | undefined;
  if (session) {
    f.session_sessions_dir = String(session.sessions_dir ?? f.session_sessions_dir);
    f.session_max_history = Number(session.max_history ?? f.session_max_history);
    f.session_summarize_on_trim = session.summarize_on_trim !== false;
    f.session_ttl_hours = Number(session.ttl_hours ?? f.session_ttl_hours);
    f.session_summary_model = String(session.summary_model ?? f.session_summary_model);
    f.session_summary_max_tokens = Number(session.summary_max_tokens ?? f.session_summary_max_tokens);
    f.session_max_context_tokens = Number(session.max_context_tokens ?? f.session_max_context_tokens);
    f.session_encryption_key = String(session.encryption_key ?? '');
  }

  const workspace = config.workspace as Record<string, unknown> | undefined;
  if (workspace) {
    f.workspace_path = String(workspace.path ?? f.workspace_path);
    f.workspace_timezone = String(workspace.timezone ?? f.workspace_timezone);
    f.workspace_memory_days = Number(workspace.memory_days ?? f.workspace_memory_days);
    f.workspace_memory_max_chars = Number(workspace.memory_max_chars ?? f.workspace_memory_max_chars);
    f.workspace_max_chars_per_file = Number(workspace.max_chars_per_file ?? f.workspace_max_chars_per_file);
    f.workspace_compact_after_days = Number(workspace.compact_after_days ?? f.workspace_compact_after_days);
    f.workspace_max_daily_entries = Number(workspace.max_daily_entries ?? f.workspace_max_daily_entries);
    f.workspace_max_long_term_lines = Number(workspace.max_long_term_lines ?? f.workspace_max_long_term_lines);
  }

  const qh = config.quiet_hours as Record<string, unknown> | undefined;
  if (qh) {
    f.quiet_hours_enabled = qh.enabled === true;
    f.quiet_hours_start = String(qh.start ?? f.quiet_hours_start);
    f.quiet_hours_end = String(qh.end ?? f.quiet_hours_end);
    f.quiet_hours_timezone = String(qh.timezone ?? f.quiet_hours_timezone);
    f.quiet_hours_allow_urgent = qh.allow_urgent !== false;
  }

  const bridge = config.bridge as Record<string, unknown> | undefined;
  if (bridge) {
    f.bridge_enabled = bridge.enabled === true;
    f.bridge_url = String(bridge.url ?? f.bridge_url);
    f.bridge_token = String(bridge.token ?? '');
  }

  const browser = config.browser as Record<string, unknown> | undefined;
  if (browser) {
    f.browser_enabled = browser.enabled === true;
    f.browser_default_mode = String(browser.default_mode ?? f.browser_default_mode);
    f.browser_cdp_url = String(browser.cdp_url ?? '');
    f.browser_max_sessions = Number(browser.max_sessions ?? f.browser_max_sessions);
    f.browser_session_timeout_minutes = Number(browser.session_timeout_minutes ?? f.browser_session_timeout_minutes);
    f.browser_headless = browser.headless !== false;
    f.browser_container_memory = String(browser.container_memory ?? f.browser_container_memory);
    f.browser_container_shm_size = String(browser.container_shm_size ?? f.browser_container_shm_size);
    f.browser_container_tmpfs_size = String(browser.container_tmpfs_size ?? f.browser_container_tmpfs_size);
    f.browser_navigate_timeout_ms = Number(browser.navigate_timeout_ms ?? f.browser_navigate_timeout_ms);
    f.browser_snapshot_timeout_ms = Number(browser.snapshot_timeout_ms ?? f.browser_snapshot_timeout_ms);
    f.browser_block_heavy_resources = browser.block_heavy_resources !== false;
  }

  const guardian = config.guardian as Record<string, unknown> | undefined;
  if (guardian) {
    f.guardian_enabled = guardian.enabled !== false;
    f.guardian_debug = guardian.debug === true;
    const fc = get(guardian, 'fast_classifier') as Record<string, unknown> | undefined;
    if (fc) {
      f.guardian_fc_enabled = fc.enabled !== false;
      f.guardian_fc_threshold = Number(fc.threshold ?? f.guardian_fc_threshold);
    }
    const lj = get(guardian, 'llm_judge') as Record<string, unknown> | undefined;
    if (lj) {
      f.guardian_lj_enabled = lj.enabled !== false;
      f.guardian_lj_model = String(lj.model ?? f.guardian_lj_model);
    }
    const policy = get(guardian, 'policy') as Record<string, unknown> | undefined;
    if (policy) {
      f.guardian_policy_enabled = policy.enabled !== false;
      f.guardian_policy_file = String(policy.policy_file ?? f.guardian_policy_file);
    }
    const audit = get(guardian, 'audit') as Record<string, unknown> | undefined;
    if (audit) {
      f.guardian_audit_enabled = audit.enabled !== false;
      f.guardian_audit_log_file = String(audit.log_file ?? f.guardian_audit_log_file);
    }
  }

  // Daemon-level fields (may be at top level in config)
  const daemon = config.daemon as Record<string, unknown> | undefined;
  if (daemon) {
    f.daemon_log_level = String(daemon.log_level ?? f.daemon_log_level);
    f.daemon_port = Number(daemon.port ?? f.daemon_port);
  }

  return f;
}

function formToConfig(form: ConfigForm, original: Record<string, unknown>): Record<string, unknown> {
  // Build config JSON from form fields, preserving unknown keys from original
  const config: Record<string, unknown> = { ...original };

  config.system_prompt = form.system_prompt;
  if (form.system_prompt_file) config.system_prompt_file = form.system_prompt_file;
  else delete config.system_prompt_file;

  config.llm = {
    model: form.llm_model,
    max_tokens: form.llm_max_tokens,
    ...(form.llm_secrets ? { secrets: form.llm_secrets } : {}),
  };

  config.agent = { max_turns: form.agent_max_turns };

  config.session = {
    sessions_dir: form.session_sessions_dir,
    max_history: form.session_max_history,
    summarize_on_trim: form.session_summarize_on_trim,
    ttl_hours: form.session_ttl_hours,
    summary_model: form.session_summary_model,
    summary_max_tokens: form.session_summary_max_tokens,
    max_context_tokens: form.session_max_context_tokens,
    ...(form.session_encryption_key ? { encryption_key: form.session_encryption_key } : {}),
  };

  config.workspace = {
    path: form.workspace_path,
    timezone: form.workspace_timezone,
    memory_days: form.workspace_memory_days,
    memory_max_chars: form.workspace_memory_max_chars,
    max_chars_per_file: form.workspace_max_chars_per_file,
    compact_after_days: form.workspace_compact_after_days,
    max_daily_entries: form.workspace_max_daily_entries,
    max_long_term_lines: form.workspace_max_long_term_lines,
  };

  config.quiet_hours = {
    enabled: form.quiet_hours_enabled,
    start: form.quiet_hours_start,
    end: form.quiet_hours_end,
    timezone: form.quiet_hours_timezone,
    allow_urgent: form.quiet_hours_allow_urgent,
  };

  config.bridge = {
    url: form.bridge_url,
    ...(form.bridge_token ? { token: form.bridge_token } : {}),
    enabled: form.bridge_enabled,
  };

  config.browser = {
    enabled: form.browser_enabled,
    default_mode: form.browser_default_mode,
    ...(form.browser_cdp_url ? { cdp_url: form.browser_cdp_url } : {}),
    max_sessions: form.browser_max_sessions,
    session_timeout_minutes: form.browser_session_timeout_minutes,
    headless: form.browser_headless,
    container_memory: form.browser_container_memory,
    container_shm_size: form.browser_container_shm_size,
    container_tmpfs_size: form.browser_container_tmpfs_size,
    navigate_timeout_ms: form.browser_navigate_timeout_ms,
    snapshot_timeout_ms: form.browser_snapshot_timeout_ms,
    block_heavy_resources: form.browser_block_heavy_resources,
  };

  if (form.guardian_enabled || original.guardian) {
    config.guardian = {
      enabled: form.guardian_enabled,
      debug: form.guardian_debug,
      fast_classifier: {
        enabled: form.guardian_fc_enabled,
        threshold: form.guardian_fc_threshold,
      },
      llm_judge: {
        enabled: form.guardian_lj_enabled,
        model: form.guardian_lj_model,
      },
      policy: {
        enabled: form.guardian_policy_enabled,
        policy_file: form.guardian_policy_file,
      },
      audit: {
        enabled: form.guardian_audit_enabled,
        log_file: form.guardian_audit_log_file,
      },
    };
  }

  return config;
}

// Count differences between two plain objects (shallow top-level comparison)
function countChanges(a: string, b: string): number {
  if (a === b) return 0;
  try {
    const objA = yaml.load(a) as Record<string, unknown> | null;
    const objB = yaml.load(b) as Record<string, unknown> | null;
    if (!objA || !objB) return a !== b ? 1 : 0;
    const allKeys = new Set([...Object.keys(objA), ...Object.keys(objB)]);
    let count = 0;
    for (const key of allKeys) {
      if (JSON.stringify(objA[key]) !== JSON.stringify(objB[key])) count++;
    }
    return count || (a !== b ? 1 : 0);
  } catch {
    return a !== b ? 1 : 0;
  }
}

// ---- CodeMirror YAML editor ----

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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [darkMode]);

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
        minHeight: 400,
        '& .cm-editor': { height: '100%', minHeight: 400 },
        '& .cm-scroller': { overflow: 'auto' },
      }}
    />
  );
}

// ---- Reusable form field components ----

function FormTextField({
  label,
  value,
  onChange,
  helperText,
  type,
  multiline,
  rows,
  error,
}: {
  label: string;
  value: string | number;
  onChange: (val: string) => void;
  helperText?: string;
  type?: string;
  multiline?: boolean;
  rows?: number;
  error?: string;
}) {
  return (
    <TextField
      label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      size="small"
      fullWidth
      type={type}
      multiline={multiline}
      rows={rows}
      helperText={error || helperText}
      error={!!error}
    />
  );
}

function FormSwitch({
  label,
  checked,
  onChange,
  helperText,
}: {
  label: string;
  checked: boolean;
  onChange: (val: boolean) => void;
  helperText?: string;
}) {
  return (
    <Box>
      <FormControlLabel
        control={<Switch checked={checked} onChange={(e) => onChange(e.target.checked)} />}
        label={label}
      />
      {helperText && <FormHelperText sx={{ mt: -0.5, ml: 7 }}>{helperText}</FormHelperText>}
    </Box>
  );
}

function FormSelect({
  label,
  value,
  onChange,
  options,
  helperText,
}: {
  label: string;
  value: string;
  onChange: (val: string) => void;
  options: { label: string; value: string }[];
  helperText?: string;
}) {
  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 0.5 }}>
        {label}
      </Typography>
      <Select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        size="small"
        fullWidth
      >
        {options.map((o) => (
          <MenuItem key={o.value} value={o.value}>
            {o.label}
          </MenuItem>
        ))}
      </Select>
      {helperText && <FormHelperText>{helperText}</FormHelperText>}
    </Box>
  );
}

// ---- Schema description helper ----

function getSchemaDescription(schema: Record<string, unknown>, path: string[]): string {
  let current: unknown = schema;
  for (const key of path) {
    if (!current || typeof current !== 'object') return '';
    const obj = current as Record<string, unknown>;
    const props = obj.properties as Record<string, unknown> | undefined;
    if (props && key in props) {
      current = props[key];
    } else {
      return '';
    }
  }
  if (current && typeof current === 'object') {
    return ((current as Record<string, unknown>).description as string) ?? '';
  }
  return '';
}

// ---- Main component ----

export default function ConfigPage() {
  const { mode: themeMode } = useThemeMode();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState(0);
  const [form, setForm] = useState<ConfigForm>(defaultForm());
  const [rawYaml, setRawYaml] = useState('');
  const [savedYaml, setSavedYaml] = useState('');
  const [originalConfig, setOriginalConfig] = useState<Record<string, unknown>>({});
  const [schema, setSchema] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [snackbar, setSnackbar] = useState<string | null>(null);
  const [applyOpen, setApplyOpen] = useState(false);
  const [applying, setApplying] = useState(false);
  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({});

  const mountedRef = useRef(true);

  const loadConfig = useCallback(async () => {
    try {
      const [configRes, schemaRes] = await Promise.all([
        fetchConfig(),
        fetchConfigSchema(),
      ]);
      if (!mountedRef.current) return;
      setRawYaml(configRes.raw_yaml);
      setSavedYaml(configRes.raw_yaml);
      setOriginalConfig(configRes.config);
      setForm(configToForm(configRes.config));
      setSchema(schemaRes);
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : 'Failed to load config');
      }
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    loadConfig();
    return () => { mountedRef.current = false; };
  }, [loadConfig]);

  // Dirty tracking
  const isDirty = rawYaml !== savedYaml || (tab === 0 && yaml.dump(formToConfig(form, originalConfig), { lineWidth: -1, sortKeys: false }) !== yaml.dump(originalConfig, { lineWidth: -1, sortKeys: false }));
  const changeCount = countChanges(
    tab === 0 ? yaml.dump(formToConfig(form, originalConfig), { lineWidth: -1, sortKeys: false }) : rawYaml,
    savedYaml,
  );

  // Unsaved changes warning
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => { if (isDirty) e.preventDefault(); };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [isDirty]);

  // Tab switching: sync form <-> raw
  const handleTabChange = (_: React.SyntheticEvent, newTab: number) => {
    if (newTab === 1 && tab === 0) {
      // Form -> Raw: serialize form to YAML
      const config = formToConfig(form, originalConfig);
      setRawYaml(yaml.dump(config, { lineWidth: -1, sortKeys: false }));
    } else if (newTab === 0 && tab === 1) {
      // Raw -> Form: parse YAML to form
      try {
        const parsed = yaml.load(rawYaml);
        if (parsed && typeof parsed === 'object') {
          setForm(configToForm(parsed as Record<string, unknown>));
          setOriginalConfig(parsed as Record<string, unknown>);
        }
      } catch {
        // Keep old form on parse failure
      }
    }
    setTab(newTab);
  };

  const updateField = <K extends keyof ConfigForm>(key: K, value: ConfigForm[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    // Clear validation error for this field
    setValidationErrors((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  // Validate form
  const validate = (): boolean => {
    const errors: Record<string, string> = {};
    if (!form.system_prompt.trim() && !form.system_prompt_file.trim()) {
      errors.system_prompt = 'System prompt or system prompt file is required';
    }
    if (form.llm_max_tokens < 1) errors.llm_max_tokens = 'Must be at least 1';
    if (form.agent_max_turns < 1 || form.agent_max_turns > 50) errors.agent_max_turns = 'Must be between 1 and 50';
    setValidationErrors(errors);
    return Object.keys(errors).length === 0;
  };

  // Save
  const handleSave = async () => {
    if (tab === 0 && !validate()) return;

    setSaving(true);
    setError(null);
    try {
      if (tab === 0) {
        const config = formToConfig(form, originalConfig);
        const result = await updateConfig({ json: config });
        setRawYaml(result.raw_yaml);
        setSavedYaml(result.raw_yaml);
        setOriginalConfig(result.config);
      } else {
        const result = await updateConfig({ raw_yaml: rawYaml });
        setSavedYaml(result.raw_yaml);
        setOriginalConfig(result.config);
        setForm(configToForm(result.config));
      }
      setSnackbar('Config saved');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  // Apply (save + restart)
  const handleApply = async () => {
    setApplying(true);
    setError(null);
    try {
      // Save first
      if (tab === 0) {
        const config = formToConfig(form, originalConfig);
        const result = await updateConfig({ json: config });
        setRawYaml(result.raw_yaml);
        setSavedYaml(result.raw_yaml);
        setOriginalConfig(result.config);
      } else {
        const result = await updateConfig({ raw_yaml: rawYaml });
        setSavedYaml(result.raw_yaml);
        setOriginalConfig(result.config);
        setForm(configToForm(result.config));
      }
      // Then apply
      await applyConfig();
      setSnackbar('Config saved and restart requested');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Apply failed');
    } finally {
      setApplying(false);
      setApplyOpen(false);
    }
  };

  // Schema description helper bound to current schema
  const desc = (...path: string[]) => getSchemaDescription(schema, path);

  if (loading) {
    return (
      <Box>
        <Skeleton width={200} height={40} />
        <Skeleton width="100%" height={48} sx={{ mt: 2 }} />
        <Skeleton width="100%" height={400} sx={{ mt: 1 }} />
      </Box>
    );
  }

  return (
    <Box>
      {/* Header */}
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 2 }}>
        <Typography variant="h5">Configuration</Typography>
        <Stack direction="row" spacing={1} alignItems="center">
          {changeCount > 0 && (
            <Chip label={`${changeCount} change${changeCount > 1 ? 's' : ''}`} color="warning" size="small" />
          )}
          <Button
            variant="outlined"
            startIcon={<RestartAltIcon />}
            onClick={() => setApplyOpen(true)}
            disabled={applying}
          >
            Apply
          </Button>
          <Button
            variant="contained"
            startIcon={<SaveIcon />}
            onClick={handleSave}
            disabled={saving || !isDirty}
          >
            {saving ? 'Saving...' : 'Save'}
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
        <Box sx={{ maxWidth: 720 }}>
          {/* Agent Section */}
          <Accordion defaultExpanded>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="subtitle1" fontWeight="bold">Agent</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack spacing={2}>
                <FormTextField
                  label="System Prompt"
                  value={form.system_prompt}
                  onChange={(v) => updateField('system_prompt', v)}
                  multiline
                  rows={4}
                  helperText={desc('system_prompt') || 'The system prompt sent to the LLM'}
                  error={validationErrors.system_prompt}
                />
                <FormTextField
                  label="System Prompt File"
                  value={form.system_prompt_file}
                  onChange={(v) => updateField('system_prompt_file', v)}
                  helperText="Path to a file containing the system prompt (overrides inline prompt)"
                />
                <FormTextField
                  label="Model"
                  value={form.llm_model}
                  onChange={(v) => updateField('llm_model', v)}
                  helperText="LLM model identifier (e.g. claude-sonnet-4-20250514)"
                />
                <FormTextField
                  label="Max Tokens"
                  value={form.llm_max_tokens}
                  onChange={(v) => updateField('llm_max_tokens', Number(v) || 0)}
                  type="number"
                  helperText="Maximum tokens for LLM response"
                  error={validationErrors.llm_max_tokens}
                />
                <FormTextField
                  label="Secrets"
                  value={form.llm_secrets}
                  onChange={(v) => updateField('llm_secrets', v)}
                  helperText="Path to age-encrypted secrets file"
                />
                <FormTextField
                  label="Max Agent Turns"
                  value={form.agent_max_turns}
                  onChange={(v) => updateField('agent_max_turns', Number(v) || 1)}
                  type="number"
                  helperText="Maximum tool-call turns per agent invocation (1-50)"
                  error={validationErrors.agent_max_turns}
                />
              </Stack>
            </AccordionDetails>
          </Accordion>

          {/* Channels Section */}
          <Accordion>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="subtitle1" fontWeight="bold">Channels</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                Channel configuration is best edited in Raw YAML mode due to its dynamic structure.
                Switch to the Raw tab to edit channel settings directly.
              </Typography>
            </AccordionDetails>
          </Accordion>

          {/* Session Section */}
          <Accordion>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="subtitle1" fontWeight="bold">Session</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack spacing={2}>
                <FormTextField
                  label="Sessions Directory"
                  value={form.session_sessions_dir}
                  onChange={(v) => updateField('session_sessions_dir', v)}
                  helperText="Directory for session storage"
                />
                <FormTextField
                  label="Max History"
                  value={form.session_max_history}
                  onChange={(v) => updateField('session_max_history', Number(v) || 0)}
                  type="number"
                  helperText="Maximum message history entries per session"
                />
                <FormSwitch
                  label="Summarize on Trim"
                  checked={form.session_summarize_on_trim}
                  onChange={(v) => updateField('session_summarize_on_trim', v)}
                  helperText="Summarize trimmed context for continuity"
                />
                <FormTextField
                  label="TTL (hours)"
                  value={form.session_ttl_hours}
                  onChange={(v) => updateField('session_ttl_hours', Number(v) || 0)}
                  type="number"
                  helperText="Session time-to-live in hours (0 = no expiry)"
                />
                <FormTextField
                  label="Summary Model"
                  value={form.session_summary_model}
                  onChange={(v) => updateField('session_summary_model', v)}
                  helperText="Model used for context summarization"
                />
                <FormTextField
                  label="Summary Max Tokens"
                  value={form.session_summary_max_tokens}
                  onChange={(v) => updateField('session_summary_max_tokens', Number(v) || 0)}
                  type="number"
                  helperText="Max tokens for summary output"
                />
                <FormTextField
                  label="Max Context Tokens"
                  value={form.session_max_context_tokens}
                  onChange={(v) => updateField('session_max_context_tokens', Number(v) || 0)}
                  type="number"
                  helperText="Maximum tokens in context window"
                />
                <FormTextField
                  label="Encryption Key"
                  value={form.session_encryption_key}
                  onChange={(v) => updateField('session_encryption_key', v)}
                  helperText="Age encryption key for session data"
                />
              </Stack>
            </AccordionDetails>
          </Accordion>

          {/* Workspace Section */}
          <Accordion>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="subtitle1" fontWeight="bold">Workspace</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack spacing={2}>
                <FormTextField
                  label="Path"
                  value={form.workspace_path}
                  onChange={(v) => updateField('workspace_path', v)}
                  helperText="Workspace directory path"
                />
                <FormTextField
                  label="Timezone"
                  value={form.workspace_timezone}
                  onChange={(v) => updateField('workspace_timezone', v)}
                  helperText="Timezone for workspace operations (e.g. UTC, America/New_York)"
                />
                <FormTextField
                  label="Memory Days"
                  value={form.workspace_memory_days}
                  onChange={(v) => updateField('workspace_memory_days', Number(v) || 0)}
                  type="number"
                  helperText="Number of days of memory to include in context"
                />
                <FormTextField
                  label="Memory Max Characters"
                  value={form.workspace_memory_max_chars}
                  onChange={(v) => updateField('workspace_memory_max_chars', Number(v) || 0)}
                  type="number"
                  helperText="Maximum characters of memory content"
                />
                <FormTextField
                  label="Max Characters per File"
                  value={form.workspace_max_chars_per_file}
                  onChange={(v) => updateField('workspace_max_chars_per_file', Number(v) || 0)}
                  type="number"
                  helperText="Maximum characters per workspace file"
                />
                <FormTextField
                  label="Compact After Days"
                  value={form.workspace_compact_after_days}
                  onChange={(v) => updateField('workspace_compact_after_days', Number(v) || 0)}
                  type="number"
                  helperText="Compact memory entries older than this many days"
                />
                <FormTextField
                  label="Max Daily Entries"
                  value={form.workspace_max_daily_entries}
                  onChange={(v) => updateField('workspace_max_daily_entries', Number(v) || 0)}
                  type="number"
                  helperText="Maximum memory entries per day"
                />
                <FormTextField
                  label="Max Long-term Lines"
                  value={form.workspace_max_long_term_lines}
                  onChange={(v) => updateField('workspace_max_long_term_lines', Number(v) || 0)}
                  type="number"
                  helperText="Maximum lines in long-term memory"
                />
              </Stack>
            </AccordionDetails>
          </Accordion>

          {/* Guardian Section */}
          <Accordion>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="subtitle1" fontWeight="bold">Guardian</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack spacing={2}>
                <FormSwitch
                  label="Enabled"
                  checked={form.guardian_enabled}
                  onChange={(v) => updateField('guardian_enabled', v)}
                  helperText="Enable the prompt-injection detection pipeline"
                />
                <FormSwitch
                  label="Debug"
                  checked={form.guardian_debug}
                  onChange={(v) => updateField('guardian_debug', v)}
                  helperText="Enable verbose guardian debug logging"
                />
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>Fast Classifier</Typography>
                <FormSwitch
                  label="Fast Classifier Enabled"
                  checked={form.guardian_fc_enabled}
                  onChange={(v) => updateField('guardian_fc_enabled', v)}
                  helperText="Local DeBERTa prompt-injection classifier"
                />
                <FormTextField
                  label="Threshold"
                  value={form.guardian_fc_threshold}
                  onChange={(v) => updateField('guardian_fc_threshold', Number(v) || 0)}
                  type="number"
                  helperText="Classification confidence threshold (0.0-1.0)"
                />
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>LLM Judge</Typography>
                <FormSwitch
                  label="LLM Judge Enabled"
                  checked={form.guardian_lj_enabled}
                  onChange={(v) => updateField('guardian_lj_enabled', v)}
                  helperText="LLM-based secondary judge for uncertain classifications"
                />
                <FormTextField
                  label="Judge Model"
                  value={form.guardian_lj_model}
                  onChange={(v) => updateField('guardian_lj_model', v)}
                  helperText="Model used for LLM judge decisions"
                />
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>Policy</Typography>
                <FormSwitch
                  label="Policy Engine Enabled"
                  checked={form.guardian_policy_enabled}
                  onChange={(v) => updateField('guardian_policy_enabled', v)}
                  helperText="YAML-based tool access policy engine"
                />
                <FormTextField
                  label="Policy File"
                  value={form.guardian_policy_file}
                  onChange={(v) => updateField('guardian_policy_file', v)}
                  helperText="Path to the policy YAML file"
                />
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>Audit</Typography>
                <FormSwitch
                  label="Audit Logging Enabled"
                  checked={form.guardian_audit_enabled}
                  onChange={(v) => updateField('guardian_audit_enabled', v)}
                  helperText="Write JSONL audit log of all guardian decisions"
                />
                <FormTextField
                  label="Audit Log File"
                  value={form.guardian_audit_log_file}
                  onChange={(v) => updateField('guardian_audit_log_file', v)}
                  helperText="Path to the audit log file"
                />
              </Stack>
            </AccordionDetails>
          </Accordion>

          {/* Quiet Hours Section */}
          <Accordion>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="subtitle1" fontWeight="bold">Quiet Hours</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack spacing={2}>
                <FormSwitch
                  label="Enabled"
                  checked={form.quiet_hours_enabled}
                  onChange={(v) => updateField('quiet_hours_enabled', v)}
                  helperText="Suppress proactive notifications during specified times"
                />
                <FormTextField
                  label="Start Time"
                  value={form.quiet_hours_start}
                  onChange={(v) => updateField('quiet_hours_start', v)}
                  helperText="Start of quiet hours (24h format, e.g. 23:00)"
                />
                <FormTextField
                  label="End Time"
                  value={form.quiet_hours_end}
                  onChange={(v) => updateField('quiet_hours_end', v)}
                  helperText="End of quiet hours (24h format, e.g. 08:00)"
                />
                <FormTextField
                  label="Timezone"
                  value={form.quiet_hours_timezone}
                  onChange={(v) => updateField('quiet_hours_timezone', v)}
                  helperText="Timezone for quiet hours (e.g. UTC, America/New_York)"
                />
                <FormSwitch
                  label="Allow Urgent"
                  checked={form.quiet_hours_allow_urgent}
                  onChange={(v) => updateField('quiet_hours_allow_urgent', v)}
                  helperText="Allow urgent messages during quiet hours"
                />
              </Stack>
            </AccordionDetails>
          </Accordion>

          {/* Bridge Section */}
          <Accordion>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="subtitle1" fontWeight="bold">Bridge</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack spacing={2}>
                <FormSwitch
                  label="Enabled"
                  checked={form.bridge_enabled}
                  onChange={(v) => updateField('bridge_enabled', v)}
                  helperText="Enable the host bridge server for macOS-native tools"
                />
                <FormTextField
                  label="URL"
                  value={form.bridge_url}
                  onChange={(v) => updateField('bridge_url', v)}
                  helperText="Bridge server URL"
                />
                <FormTextField
                  label="Token"
                  value={form.bridge_token}
                  onChange={(v) => updateField('bridge_token', v)}
                  helperText="Authentication token for the bridge server"
                />
              </Stack>
            </AccordionDetails>
          </Accordion>

          {/* Browser Section */}
          <Accordion>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="subtitle1" fontWeight="bold">Browser</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack spacing={2}>
                <FormSwitch
                  label="Enabled"
                  checked={form.browser_enabled}
                  onChange={(v) => updateField('browser_enabled', v)}
                  helperText="Enable web browsing via Playwright CDP"
                />
                <FormSelect
                  label="Default Mode"
                  value={form.browser_default_mode}
                  onChange={(v) => updateField('browser_default_mode', v)}
                  options={[
                    { label: 'Managed', value: 'managed' },
                    { label: 'CDP', value: 'cdp' },
                  ]}
                  helperText="Browser launch mode"
                />
                <FormTextField
                  label="CDP URL"
                  value={form.browser_cdp_url}
                  onChange={(v) => updateField('browser_cdp_url', v)}
                  helperText="Chrome DevTools Protocol URL (for CDP mode)"
                />
                <FormTextField
                  label="Max Sessions"
                  value={form.browser_max_sessions}
                  onChange={(v) => updateField('browser_max_sessions', Number(v) || 1)}
                  type="number"
                  helperText="Maximum concurrent browser sessions"
                />
                <FormTextField
                  label="Session Timeout (minutes)"
                  value={form.browser_session_timeout_minutes}
                  onChange={(v) => updateField('browser_session_timeout_minutes', Number(v) || 1)}
                  type="number"
                  helperText="Idle session timeout in minutes"
                />
                <FormSwitch
                  label="Headless"
                  checked={form.browser_headless}
                  onChange={(v) => updateField('browser_headless', v)}
                  helperText="Run browser in headless mode"
                />
                <FormSwitch
                  label="Block Heavy Resources"
                  checked={form.browser_block_heavy_resources}
                  onChange={(v) => updateField('browser_block_heavy_resources', v)}
                  helperText="Block images, fonts, and media for faster page loads"
                />
                <FormTextField
                  label="Container Memory"
                  value={form.browser_container_memory}
                  onChange={(v) => updateField('browser_container_memory', v)}
                  helperText="Docker container memory limit (e.g. 1024m)"
                />
                <FormTextField
                  label="Navigate Timeout (ms)"
                  value={form.browser_navigate_timeout_ms}
                  onChange={(v) => updateField('browser_navigate_timeout_ms', Number(v) || 0)}
                  type="number"
                  helperText="Page navigation timeout in milliseconds"
                />
                <FormTextField
                  label="Snapshot Timeout (ms)"
                  value={form.browser_snapshot_timeout_ms}
                  onChange={(v) => updateField('browser_snapshot_timeout_ms', Number(v) || 0)}
                  type="number"
                  helperText="Page snapshot timeout in milliseconds"
                />
              </Stack>
            </AccordionDetails>
          </Accordion>

          {/* Daemon Section */}
          <Accordion>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="subtitle1" fontWeight="bold">Daemon</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack spacing={2}>
                <FormSelect
                  label="Log Level"
                  value={form.daemon_log_level}
                  onChange={(v) => updateField('daemon_log_level', v)}
                  options={[
                    { label: 'DEBUG', value: 'DEBUG' },
                    { label: 'INFO', value: 'INFO' },
                    { label: 'WARNING', value: 'WARNING' },
                    { label: 'ERROR', value: 'ERROR' },
                  ]}
                  helperText="Daemon logging verbosity"
                />
                <FormTextField
                  label="Port"
                  value={form.daemon_port}
                  onChange={(v) => updateField('daemon_port', Number(v) || 8099)}
                  type="number"
                  helperText="HTTP port for the daemon API"
                />
              </Stack>
            </AccordionDetails>
          </Accordion>

          {/* Tools Section */}
          <Accordion>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="subtitle1" fontWeight="bold">Tools</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Typography variant="body2" color="text.secondary">
                Tool configuration is complex and best edited in Raw YAML mode.
                Switch to the Raw tab to edit tool definitions directly.
              </Typography>
            </AccordionDetails>
          </Accordion>
        </Box>
      )}

      {/* Raw YAML tab */}
      {tab === 1 && (
        <YamlEditor
          value={rawYaml}
          onChange={setRawYaml}
          darkMode={themeMode === 'dark'}
        />
      )}

      {/* Apply confirmation dialog */}
      <Dialog open={applyOpen} onClose={() => setApplyOpen(false)}>
        <DialogTitle>Apply Configuration</DialogTitle>
        <DialogContent>
          <DialogContentText>
            This will save the configuration and restart the daemon.
            Active sessions will be interrupted. Continue?
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setApplyOpen(false)} disabled={applying}>
            Cancel
          </Button>
          <Button
            onClick={handleApply}
            color="warning"
            variant="contained"
            disabled={applying}
          >
            {applying ? 'Applying...' : 'Apply & Restart'}
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
