# Creel Dashboard

Web dashboard for managing the Creel AI agent framework. Built with React, TypeScript, MUI v6, and Vite.

## Prerequisites

- Node.js 18+ and npm

## Development

Install dependencies:

```bash
npm install
```

Start the dev server (proxies API calls to the daemon at `localhost:8099`):

```bash
npm run dev
```

The dev server runs at `http://localhost:5173` with hot module replacement.

## Building

Build the production bundle:

```bash
npm run build
```

Output is written to `dist/`. To deploy into the daemon's static file serving:

```bash
# From the repo root:
scripts/build-dashboard.sh
```

This runs `npm ci`, builds, and copies `dist/` to `src/taskrunner/dashboard_static/` where the daemon serves it.

## Project Structure

```
src/
  api/client.ts       — Typed fetch wrapper and API methods
  pages/              — Page components (Overview, Tasks, Cron, Files, Config, Logs)
  App.tsx             — Routes and layout
  Layout.tsx          — AppBar, sidebar drawer, responsive shell
  ThemeContext.tsx     — MUI dark/light mode provider
  AuthContext.tsx      — Dashboard token authentication
  theme.ts            — MUI theme factory
  main.tsx            — Entry point
```
