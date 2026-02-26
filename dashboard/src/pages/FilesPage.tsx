import { useCallback, useEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';

import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Breadcrumbs from '@mui/material/Breadcrumbs';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import Divider from '@mui/material/Divider';
import IconButton from '@mui/material/IconButton';
import List from '@mui/material/List';
import ListItemButton from '@mui/material/ListItemButton';
import ListItemIcon from '@mui/material/ListItemIcon';
import ListItemText from '@mui/material/ListItemText';
import MenuItem from '@mui/material/MenuItem';
import Select from '@mui/material/Select';
import Skeleton from '@mui/material/Skeleton';
import Snackbar from '@mui/material/Snackbar';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';

import AddIcon from '@mui/icons-material/Add';
import ArticleIcon from '@mui/icons-material/Article';
import DataObjectIcon from '@mui/icons-material/DataObject';
import DescriptionIcon from '@mui/icons-material/Description';
import EditIcon from '@mui/icons-material/Edit';
import ExpandLessIcon from '@mui/icons-material/ExpandLess';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import FolderIcon from '@mui/icons-material/Folder';
import FolderOpenIcon from '@mui/icons-material/FolderOpen';
import InsertDriveFileIcon from '@mui/icons-material/InsertDriveFile';
import PreviewIcon from '@mui/icons-material/Preview';
import SaveIcon from '@mui/icons-material/Save';

import { EditorView, basicSetup } from 'codemirror';
import { EditorState } from '@codemirror/state';
import { yaml as yamlMode } from '@codemirror/lang-yaml';
import { json as jsonMode } from '@codemirror/lang-json';
import { markdown as markdownMode } from '@codemirror/lang-markdown';
import { oneDark } from '@codemirror/theme-one-dark';
import { keymap } from '@codemirror/view';

import { useThemeMode } from '../ThemeContext';
import type { FileTreeNode, FileContent } from '../api/client';
import {
  fetchFileTree,
  fetchFileContent,
  updateFile,
  createFile,
} from '../api/client';

// ---- Quick access files ----

const QUICK_ACCESS = [
  { path: 'agent.yaml', label: 'agent.yaml' },
  { path: 'workspace/SOUL.md', label: 'SOUL.md' },
  { path: 'workspace/USER.md', label: 'USER.md' },
  { path: 'workspace/MEMORY.md', label: 'MEMORY.md' },
];

// ---- File icon helper ----

function fileIcon(name: string) {
  const ext = name.includes('.') ? '.' + name.split('.').pop()!.toLowerCase() : '';
  if (ext === '.yaml' || ext === '.yml') return <DescriptionIcon fontSize="small" />;
  if (ext === '.md') return <ArticleIcon fontSize="small" />;
  if (ext === '.json' || ext === '.jsonl') return <DataObjectIcon fontSize="small" />;
  return <InsertDriveFileIcon fontSize="small" />;
}

// ---- Language mode by extension ----

function langExtension(filePath: string) {
  if (filePath.endsWith('.yaml') || filePath.endsWith('.yml')) return yamlMode();
  if (filePath.endsWith('.json') || filePath.endsWith('.jsonl')) return jsonMode();
  if (filePath.endsWith('.md')) return markdownMode();
  return [];
}

// ---- New file templates ----

const FILE_TEMPLATES = [
  { label: 'Blank', value: '' },
  {
    label: 'YAML Task',
    value: `name: new-task
schedule: "0 9 * * *"
prompt: |
  Your prompt here
output:
  type: stdout
llm:
  max_tokens: 300
enabled: true
`,
  },
  {
    label: 'Markdown Note',
    value: `# Title

Your content here.
`,
  },
];

// ---- Tree item component ----

function TreeNode({
  node,
  expanded,
  onToggle,
  selectedPath,
  onSelect,
  depth,
}: {
  node: FileTreeNode;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  selectedPath: string | null;
  onSelect: (path: string) => void;
  depth: number;
}) {
  const isDir = node.type === 'dir';
  const isOpen = expanded.has(node.path);
  const isSelected = node.path === selectedPath;

  if (isDir) {
    return (
      <>
        <ListItemButton
          onClick={() => onToggle(node.path)}
          selected={false}
          sx={{ pl: 1 + depth * 2 }}
          dense
        >
          <ListItemIcon sx={{ minWidth: 28 }}>
            {isOpen ? <FolderOpenIcon fontSize="small" color="primary" /> : <FolderIcon fontSize="small" />}
          </ListItemIcon>
          <ListItemText
            primary={node.name}
            primaryTypographyProps={{ variant: 'body2', fontWeight: 500 }}
          />
          {isOpen ? <ExpandLessIcon fontSize="small" /> : <ExpandMoreIcon fontSize="small" />}
        </ListItemButton>
        {isOpen &&
          node.children?.map((child) => (
            <TreeNode
              key={child.path}
              node={child}
              expanded={expanded}
              onToggle={onToggle}
              selectedPath={selectedPath}
              onSelect={onSelect}
              depth={depth + 1}
            />
          ))}
      </>
    );
  }

  return (
    <ListItemButton
      onClick={() => onSelect(node.path)}
      selected={isSelected}
      sx={{ pl: 1 + depth * 2 }}
      dense
    >
      <ListItemIcon sx={{ minWidth: 28 }}>
        {fileIcon(node.name)}
      </ListItemIcon>
      <ListItemText
        primary={node.name}
        primaryTypographyProps={{ variant: 'body2' }}
      />
    </ListItemButton>
  );
}

// ---- CodeMirror editor for files ----

function FileEditor({
  value,
  onChange,
  darkMode,
  filePath,
  onSave,
}: {
  value: string;
  onChange: (val: string) => void;
  darkMode: boolean;
  filePath: string;
  onSave: () => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const onSaveRef = useRef(onSave);
  onSaveRef.current = onSave;
  const internalUpdate = useRef(false);

  useEffect(() => {
    if (!containerRef.current) return;
    const extensions = [
      basicSetup,
      langExtension(filePath),
      EditorView.updateListener.of((update) => {
        if (update.docChanged) {
          internalUpdate.current = true;
          onChangeRef.current(update.state.doc.toString());
        }
      }),
      EditorView.lineWrapping,
      keymap.of([
        {
          key: 'Mod-s',
          run: () => {
            onSaveRef.current();
            return true;
          },
        },
      ]),
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
  }, [darkMode, filePath]);

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
        flex: 1,
        minHeight: 300,
        '& .cm-editor': { height: '100%', minHeight: 300 },
        '& .cm-scroller': { overflow: 'auto' },
      }}
    />
  );
}

// ---- Main component ----

export default function FilesPage() {
  const { mode: themeMode } = useThemeMode();
  const darkMode = themeMode === 'dark';

  // Tree state
  const [tree, setTree] = useState<FileTreeNode | null>(null);
  const [treeLoading, setTreeLoading] = useState(true);
  const [treeError, setTreeError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set(['']));

  // File state
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<FileContent | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);

  // Editor state
  const [editorValue, setEditorValue] = useState('');
  const [savedValue, setSavedValue] = useState('');
  const [saving, setSaving] = useState(false);
  const [mdPreview, setMdPreview] = useState(false);

  // New file dialog
  const [newFileOpen, setNewFileOpen] = useState(false);
  const [newFilePath, setNewFilePath] = useState('');
  const [newFileTemplate, setNewFileTemplate] = useState('');
  const [newFileError, setNewFileError] = useState('');

  // Snackbar
  const [snackbar, setSnackbar] = useState<string | null>(null);

  const isDirty = editorValue !== savedValue;

  // Load tree
  const loadTree = useCallback(async () => {
    try {
      const data = await fetchFileTree();
      setTree(data);
    } catch (err) {
      setTreeError(err instanceof Error ? err.message : 'Failed to load file tree');
    } finally {
      setTreeLoading(false);
    }
  }, []);

  useEffect(() => {
    loadTree();
  }, [loadTree]);

  // Unsaved changes warning
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (isDirty) e.preventDefault();
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [isDirty]);

  // Toggle directory expand
  const handleToggle = useCallback((path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }, []);

  // Select a file
  const handleSelectFile = useCallback(async (filePath: string) => {
    setSelectedPath(filePath);
    setFileLoading(true);
    setFileError(null);
    setMdPreview(false);
    try {
      const data = await fetchFileContent(filePath);
      setFileContent(data);
      if (data.content !== null) {
        setEditorValue(data.content);
        setSavedValue(data.content);
      }
    } catch (err) {
      setFileError(err instanceof Error ? err.message : 'Failed to load file');
      setFileContent(null);
    } finally {
      setFileLoading(false);
    }
  }, []);

  // Save file
  const handleSave = useCallback(async () => {
    if (!selectedPath || !isDirty) return;
    setSaving(true);
    try {
      await updateFile(selectedPath, editorValue);
      setSavedValue(editorValue);
      setSnackbar('File saved');
    } catch (err) {
      setFileError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  }, [selectedPath, editorValue, isDirty]);

  // Create new file
  const handleCreateFile = async () => {
    const path = newFilePath.trim();
    if (!path) {
      setNewFileError('Path is required');
      return;
    }
    if (path.includes('..')) {
      setNewFileError('Path traversal not allowed');
      return;
    }
    try {
      await createFile(path, newFileTemplate);
      setNewFileOpen(false);
      setNewFilePath('');
      setNewFileTemplate('');
      setNewFileError('');
      setSnackbar('File created');
      // Reload tree and open the new file
      await loadTree();
      // Expand parent directories
      const parts = path.split('/');
      setExpanded((prev) => {
        const next = new Set(prev);
        next.add('');
        for (let i = 1; i < parts.length; i++) {
          next.add(parts.slice(0, i).join('/'));
        }
        return next;
      });
      handleSelectFile(path);
    } catch (err) {
      setNewFileError(err instanceof Error ? err.message : 'Failed to create file');
    }
  };

  // Keyboard shortcut for save (global fallback)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 's') {
        e.preventDefault();
        if (selectedPath && isDirty) {
          handleSave();
        }
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [selectedPath, isDirty, handleSave]);

  // Breadcrumb from path
  const breadcrumbs = selectedPath
    ? ['~/.creel', ...selectedPath.split('/')]
    : [];

  const isMarkdown = selectedPath?.endsWith('.md');

  return (
    <Box sx={{ display: 'flex', height: 'calc(100vh - 88px)', gap: 0 }}>
      {/* Left panel: tree */}
      <Box
        sx={{
          width: 280,
          minWidth: 280,
          borderRight: 1,
          borderColor: 'divider',
          overflow: 'auto',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        {/* Quick Access */}
        <Box sx={{ px: 1, pt: 1, pb: 0.5 }}>
          <Typography variant="overline" color="text.secondary" sx={{ px: 1 }}>
            Quick Access
          </Typography>
        </Box>
        <List dense disablePadding>
          {QUICK_ACCESS.map((qa) => (
            <ListItemButton
              key={qa.path}
              onClick={() => handleSelectFile(qa.path)}
              selected={selectedPath === qa.path}
              sx={{ pl: 2 }}
              dense
            >
              <ListItemIcon sx={{ minWidth: 28 }}>
                {fileIcon(qa.label)}
              </ListItemIcon>
              <ListItemText
                primary={qa.label}
                primaryTypographyProps={{ variant: 'body2' }}
              />
            </ListItemButton>
          ))}
        </List>

        <Divider sx={{ my: 0.5 }} />

        {/* File tree */}
        <Box sx={{ px: 1, pt: 0.5, pb: 0.5, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <Typography variant="overline" color="text.secondary" sx={{ px: 1 }}>
            Files
          </Typography>
          <IconButton size="small" onClick={() => setNewFileOpen(true)} title="New file">
            <AddIcon fontSize="small" />
          </IconButton>
        </Box>
        <List dense disablePadding sx={{ flex: 1, overflow: 'auto' }}>
          {treeLoading && (
            <Box sx={{ p: 2 }}>
              <Skeleton width="60%" />
              <Skeleton width="80%" />
              <Skeleton width="50%" />
              <Skeleton width="70%" />
            </Box>
          )}
          {treeError && (
            <Alert severity="error" sx={{ m: 1 }}>
              {treeError}
            </Alert>
          )}
          {tree &&
            tree.children?.map((child) => (
              <TreeNode
                key={child.path}
                node={child}
                expanded={expanded}
                onToggle={handleToggle}
                selectedPath={selectedPath}
                onSelect={handleSelectFile}
                depth={0}
              />
            ))}
        </List>
      </Box>

      {/* Right panel: editor */}
      <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        {!selectedPath && (
          <Box
            sx={{
              flex: 1,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <Typography color="text.secondary">
              Select a file from the tree to edit
            </Typography>
          </Box>
        )}

        {selectedPath && (
          <>
            {/* Toolbar */}
            <Stack
              direction="row"
              alignItems="center"
              justifyContent="space-between"
              sx={{ px: 2, py: 1, borderBottom: 1, borderColor: 'divider' }}
            >
              <Breadcrumbs separator="/" maxItems={5}>
                {breadcrumbs.map((part, i) => (
                  <Typography
                    key={i}
                    variant="body2"
                    color={i === breadcrumbs.length - 1 ? 'text.primary' : 'text.secondary'}
                  >
                    {part}
                    {i === breadcrumbs.length - 1 && isDirty && (
                      <Chip
                        label="modified"
                        size="small"
                        color="warning"
                        variant="outlined"
                        sx={{ ml: 1, height: 20 }}
                      />
                    )}
                  </Typography>
                ))}
              </Breadcrumbs>

              <Stack direction="row" spacing={1} alignItems="center">
                {isMarkdown && (
                  <IconButton
                    size="small"
                    onClick={() => setMdPreview((p) => !p)}
                    color={mdPreview ? 'primary' : 'default'}
                    title={mdPreview ? 'Switch to editor' : 'Switch to preview'}
                  >
                    {mdPreview ? <EditIcon fontSize="small" /> : <PreviewIcon fontSize="small" />}
                  </IconButton>
                )}
                <Button
                  size="small"
                  variant="contained"
                  startIcon={<SaveIcon />}
                  onClick={handleSave}
                  disabled={saving || !isDirty}
                >
                  {saving ? 'Saving…' : 'Save'}
                </Button>
              </Stack>
            </Stack>

            {/* Content */}
            <Box sx={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column' }}>
              {fileLoading && (
                <Box sx={{ p: 2 }}>
                  <Skeleton width="100%" height={24} />
                  <Skeleton width="80%" height={24} />
                  <Skeleton width="90%" height={24} />
                  <Skeleton width="60%" height={24} />
                </Box>
              )}

              {fileError && (
                <Alert severity="error" sx={{ m: 2 }}>
                  {fileError}
                </Alert>
              )}

              {!fileLoading && fileContent?.binary && (
                <Box
                  sx={{
                    flex: 1,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  <Typography color="text.secondary">
                    Binary file ({fileContent.size_bytes.toLocaleString()} bytes) — cannot be edited
                  </Typography>
                </Box>
              )}

              {!fileLoading && fileContent && !fileContent.binary && (
                <>
                  {isMarkdown && mdPreview ? (
                    <Box
                      sx={{
                        p: 3,
                        flex: 1,
                        overflow: 'auto',
                        '& h1': { mt: 2, mb: 1 },
                        '& h2': { mt: 2, mb: 1 },
                        '& h3': { mt: 1.5, mb: 0.5 },
                        '& p': { mb: 1 },
                        '& code': {
                          bgcolor: 'action.hover',
                          px: 0.5,
                          py: 0.25,
                          borderRadius: 0.5,
                          fontFamily: 'monospace',
                          fontSize: '0.875em',
                        },
                        '& pre': {
                          bgcolor: 'action.hover',
                          p: 2,
                          borderRadius: 1,
                          overflow: 'auto',
                        },
                        '& pre code': {
                          bgcolor: 'transparent',
                          p: 0,
                        },
                        '& ul, & ol': { pl: 3, mb: 1 },
                        '& blockquote': {
                          borderLeft: 4,
                          borderColor: 'primary.main',
                          pl: 2,
                          ml: 0,
                          color: 'text.secondary',
                        },
                      }}
                    >
                      <Markdown>{editorValue}</Markdown>
                    </Box>
                  ) : (
                    <FileEditor
                      value={editorValue}
                      onChange={setEditorValue}
                      darkMode={darkMode}
                      filePath={selectedPath}
                      onSave={handleSave}
                    />
                  )}
                </>
              )}
            </Box>
          </>
        )}
      </Box>

      {/* New File Dialog */}
      <Dialog
        open={newFileOpen}
        onClose={() => {
          setNewFileOpen(false);
          setNewFileError('');
        }}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>New File</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="File path (relative to ~/.creel/)"
              value={newFilePath}
              onChange={(e) => {
                setNewFilePath(e.target.value);
                setNewFileError('');
              }}
              error={!!newFileError}
              helperText={newFileError || 'e.g. workspace/notes.md or tasks/my-task.yaml'}
              size="small"
              fullWidth
              autoFocus
            />
            <Select
              value={newFileTemplate}
              onChange={(e) => setNewFileTemplate(e.target.value)}
              size="small"
              displayEmpty
              fullWidth
            >
              <MenuItem value="">
                <em>Blank</em>
              </MenuItem>
              {FILE_TEMPLATES.slice(1).map((t) => (
                <MenuItem key={t.label} value={t.value}>
                  {t.label}
                </MenuItem>
              ))}
            </Select>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button
            onClick={() => {
              setNewFileOpen(false);
              setNewFileError('');
            }}
          >
            Cancel
          </Button>
          <Button variant="contained" onClick={handleCreateFile}>
            Create
          </Button>
        </DialogActions>
      </Dialog>

      {/* Snackbar */}
      <Snackbar
        open={!!snackbar}
        autoHideDuration={3000}
        onClose={() => setSnackbar(null)}
        message={snackbar}
      />
    </Box>
  );
}
