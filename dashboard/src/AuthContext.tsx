import { createContext, useContext, useState, useEffect, useCallback } from 'react';
import type { ReactNode } from 'react';
import Button from '@mui/material/Button';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogContentText from '@mui/material/DialogContentText';
import DialogTitle from '@mui/material/DialogTitle';
import TextField from '@mui/material/TextField';
import { getToken, setToken, clearToken, setOnUnauthorized } from './api/client';

interface AuthState {
  token: string | null;
  logout: () => void;
}

const AuthContext = createContext<AuthState>({ token: null, logout: () => {} });

export function useAuth(): AuthState {
  return useContext(AuthContext);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(getToken);
  const [showLogin, setShowLogin] = useState(!getToken());
  const [inputValue, setInputValue] = useState('');
  const [error, setError] = useState('');

  const handleUnauthorized = useCallback(() => {
    setShowLogin(true);
  }, []);

  useEffect(() => {
    setOnUnauthorized(handleUnauthorized);
    return () => setOnUnauthorized(null);
  }, [handleUnauthorized]);

  const handleSubmit = async () => {
    const trimmed = inputValue.trim();
    if (!trimmed) {
      setError('Token is required');
      return;
    }

    // Test the token by making a quick request
    try {
      const res = await fetch('/api/status', {
        headers: { 'Authorization': `Bearer ${trimmed}` },
      });
      if (res.status === 401) {
        setError('Invalid token');
        return;
      }
      // Token works
      setToken(trimmed);
      setTokenState(trimmed);
      setShowLogin(false);
      setInputValue('');
      setError('');
    } catch {
      // Network error — accept the token optimistically
      setToken(trimmed);
      setTokenState(trimmed);
      setShowLogin(false);
      setInputValue('');
      setError('');
    }
  };

  const logout = useCallback(() => {
    clearToken();
    setTokenState(null);
    setShowLogin(true);
  }, []);

  return (
    <AuthContext value={{ token, logout }}>
      {children}
      <Dialog
        open={showLogin}
        disableEscapeKeyDown
      >
        <DialogTitle>Dashboard Authentication</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ mb: 2 }}>
            Enter your dashboard token to continue. The token is stored in{' '}
            <code>~/.creel/dashboard-token</code> on the server.
          </DialogContentText>
          <TextField
            autoFocus
            fullWidth
            label="Token"
            type="password"
            value={inputValue}
            onChange={(e) => {
              setInputValue(e.target.value);
              setError('');
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleSubmit();
            }}
            error={!!error}
            helperText={error}
            size="small"
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={handleSubmit} variant="contained">
            Submit
          </Button>
        </DialogActions>
      </Dialog>
    </AuthContext>
  );
}
