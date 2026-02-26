import AppBar from '@mui/material/AppBar';
import Toolbar from '@mui/material/Toolbar';
import Typography from '@mui/material/Typography';
import IconButton from '@mui/material/IconButton';
import Box from '@mui/material/Box';
import Brightness4Icon from '@mui/icons-material/Brightness4';
import Brightness7Icon from '@mui/icons-material/Brightness7';
import { useThemeMode } from './ThemeContext';

function App() {
  const { mode, toggleTheme } = useThemeMode();

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <AppBar position="static">
        <Toolbar>
          <Typography variant="h6" component="div" sx={{ flexGrow: 1 }}>
            Creel Dashboard
          </Typography>
          <IconButton color="inherit" onClick={toggleTheme} aria-label="toggle dark mode">
            {mode === 'dark' ? <Brightness7Icon /> : <Brightness4Icon />}
          </IconButton>
        </Toolbar>
      </AppBar>
      <Box component="main" sx={{ flexGrow: 1, p: 3 }}>
        <Typography>Dashboard is running.</Typography>
      </Box>
    </Box>
  );
}

export default App;
