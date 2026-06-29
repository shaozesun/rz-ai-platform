import client from './client';
import type { AdminUser, Role, Application, AuditLog, PaginatedData } from '../types';

// Users
export async function getUsers(params: {
  page?: number;
  page_size?: number;
  search?: string;
  status?: string;
}): Promise<PaginatedData<AdminUser>> {
  const { data } = await client.get('/admin/users', { params });
  return data.data ?? data;
}

export async function getUser(id: string): Promise<AdminUser> {
  const { data } = await client.get(`/admin/users/${id}`);
  return data.data ?? data;
}

export async function updateUserStatus(
  id: string,
  status: string,
): Promise<{ ok: boolean }> {
  const { data } = await client.put(`/admin/users/${id}/status`, { status });
  return data;
}

export async function updateUserRoles(
  id: string,
  roles: string[],
): Promise<{ ok: boolean }> {
  const { data } = await client.put(`/admin/users/${id}/roles`, { roles });
  return data;
}

export async function updateUserPermissions(
  id: string,
  permissions: string[],
): Promise<{ ok: boolean }> {
  const { data } = await client.put(`/admin/users/${id}/permissions`, { permissions });
  return data;
}

export async function resetUserPassword(
  id: string,
): Promise<{ ok: boolean; data: { new_password: string } }> {
  const { data } = await client.post(`/admin/users/${id}/reset-password`);
  return data;
}

// Roles
export async function getRoles(): Promise<Role[]> {
  const { data } = await client.get('/admin/roles');
  return data.roles ?? data.data ?? [];
}

export async function createRole(role: {
  role_id: string;
  name: string;
  description: string;
  permissions: string[];
}): Promise<Role> {
  const { data } = await client.post('/admin/roles', role);
  return data.role ?? data;
}

export async function updateRole(
  id: string,
  updates: { name?: string; description?: string; permissions?: string[] },
): Promise<{ ok: boolean }> {
  const { data } = await client.put(`/admin/roles/${id}`, updates);
  return data;
}

// Applications
export async function getApplications(params: {
  page?: number;
  page_size?: number;
  status?: string;
  search?: string;
}): Promise<PaginatedData<Application>> {
  const { data } = await client.get('/admin/applications', { params });
  return data.data ?? data;
}

export async function approveApplication(id: string): Promise<{ ok: boolean }> {
  const { data } = await client.post(`/admin/applications/${id}/approve`);
  return data;
}

export async function batchApproveApplications(ids: string[]): Promise<{ ok: boolean; data: { ok: number; fail: number } }> {
  const { data } = await client.post('/admin/applications/batch-approve', { ids });
  return data;
}

export async function rejectApplication(
  id: string,
  reason: string,
): Promise<{ ok: boolean }> {
  const { data } = await client.post(`/admin/applications/${id}/reject`, { reason });
  return data;
}

export async function batchRejectApplications(ids: string[], reason: string): Promise<{ ok: boolean; data: { ok: number; fail: number } }> {
  const { data } = await client.post('/admin/applications/batch-reject', { ids, reason });
  return data;
}

// Audit logs
export async function getAuditLogs(params: {
  page?: number;
  page_size?: number;
  action?: string;
}): Promise<PaginatedData<AuditLog>> {
  const { data } = await client.get('/admin/audit-logs', { params });
  return data.data ?? data;
}
