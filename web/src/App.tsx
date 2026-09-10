import { useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { useAuthStore } from './stores/authStore';
import ProtectedRoute from './components/ProtectedRoute';
import LoginPage from './pages/LoginPage';
import OverviewPage from './pages/OverviewPage';
import InnovationPage from './pages/InnovationPage';
import ChatPage from './pages/ChatPage';
import VideoPage from './pages/VideoPage';
import KnowledgePage from './pages/KnowledgePage';
import RiskPage from './pages/RiskPage';
import FireSafetyPage from './pages/FireSafetyPage';
import PermissionPage from './pages/PermissionPage';
import AuthCenterPage from './pages/AuthCenterPage';
import ScheduledTasksPage from './pages/ScheduledTasksPage';
import SettingsPage from './pages/SettingsPage';
import AdminLayout from './pages/admin/AdminLayout';
import UsersPage from './pages/admin/UsersPage';
import RolesPage from './pages/admin/RolesPage';
import ApprovalsPage from './pages/admin/ApprovalsPage';
import AuditLogsPage from './pages/admin/AuditLogsPage';

export default function App() {
  const loadFromStorage = useAuthStore((s) => s.loadFromStorage);
  const user = useAuthStore((s) => s.user);

  useEffect(() => {
    loadFromStorage();
  }, [loadFromStorage]);

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />

        <Route path="/" element={
          <ProtectedRoute><OverviewPage /></ProtectedRoute>
        } />
        <Route path="/innovation" element={
          <ProtectedRoute><InnovationPage /></ProtectedRoute>
        } />
        <Route path="/assistant" element={
          <ProtectedRoute><ChatPage key={user?.user_id ?? 'guest'} /></ProtectedRoute>
        } />
        <Route path="/video" element={
          <ProtectedRoute requirePerm="ai:video"><VideoPage /></ProtectedRoute>
        } />
        <Route path="/fire-safety" element={
          <ProtectedRoute requirePerm="ai:fire_safety"><FireSafetyPage /></ProtectedRoute>
        } />
        <Route path="/knowledge" element={
          <ProtectedRoute requirePerm="ai:knowledge"><KnowledgePage /></ProtectedRoute>
        } />
        <Route path="/risk" element={
          <ProtectedRoute requirePerm="ai:risk"><RiskPage /></ProtectedRoute>
        } />
        <Route path="/scheduled-tasks" element={
          <ProtectedRoute requirePerm="ai:agent"><ScheduledTasksPage /></ProtectedRoute>
        } />

        <Route path="/permission" element={
          <ProtectedRoute><PermissionPage /></ProtectedRoute>
        } />
        <Route path="/settings" element={
          <ProtectedRoute><SettingsPage /></ProtectedRoute>
        } />
        <Route path="/auth-center" element={
          <ProtectedRoute><AuthCenterPage /></ProtectedRoute>
        } />
        <Route path="/admin" element={
          <ProtectedRoute requirePerm="system:admin"><AdminLayout /></ProtectedRoute>
        }>
          <Route index element={<UsersPage />} />
          <Route path="users" element={<UsersPage />} />
          <Route path="roles" element={<RolesPage />} />
          <Route path="approvals" element={<ApprovalsPage />} />
          <Route path="audit-logs" element={<AuditLogsPage />} />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
