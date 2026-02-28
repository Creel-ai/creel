import { Routes, Route } from 'react-router-dom';
import Layout from './Layout';
import OverviewPage from './pages/OverviewPage';
import TaskListPage from './pages/TaskListPage';
import TaskEditPage from './pages/TaskEditPage';
import CronPage from './pages/CronPage';
import FilesPage from './pages/FilesPage';
import ConfigPage from './pages/ConfigPage';
import LogsPage from './pages/LogsPage';
import NotFoundPage from './pages/NotFoundPage';

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<OverviewPage />} />
        <Route path="tasks" element={<TaskListPage />} />
        <Route path="tasks/new" element={<TaskEditPage />} />
        <Route path="tasks/:name" element={<TaskEditPage />} />
        <Route path="cron" element={<CronPage />} />
        <Route path="files" element={<FilesPage />} />
        <Route path="config" element={<ConfigPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
