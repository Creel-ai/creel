import { Routes, Route } from 'react-router-dom';
import Layout from './Layout';
import OverviewPage from './pages/OverviewPage';
import TaskListPage from './pages/TaskListPage';
import TaskDetailPage from './pages/TaskDetailPage';
import TaskNewPage from './pages/TaskNewPage';
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
        <Route path="tasks/new" element={<TaskNewPage />} />
        <Route path="tasks/:name" element={<TaskDetailPage />} />
        <Route path="cron" element={<CronPage />} />
        <Route path="files" element={<FilesPage />} />
        <Route path="config" element={<ConfigPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
