export interface User {
  user_id: string;
  phone: string;
  name: string;
  email?: string;
  company?: string;
  avatar?: string;
  user_type: 'UNVERIFIED' | 'INTERNAL' | 'EXTERNAL';
  status: 'ACTIVE' | 'DISABLED';
  roles: string[];
  permissions: string[];
  agent_enabled?: boolean;
  created_at: string;
}

export interface AuthTokens {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export interface LoginResponse {
  ok: boolean;
  tokens: AuthTokens;
  user: User;
  permissions: string[];
}

export interface Session {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface Message {
  message_id: string;
  session_id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: string;
}

export interface PlanStep {
  title: string;
  description: string;
}

export interface ExecutionPlan {
  goal: string;
  steps: PlanStep[];
}

export interface InterviewOption {
  value: string;
  label: string;
  recommended?: boolean;
}

export interface InterviewQuestion {
  id: string;
  question: string;
  options: InterviewOption[];
}

export type InterviewAnswers = Record<string, string>;

export interface HazardItem {
  category: string;
  severity: string;
  location: string;
  description: string;
  recommendation: string;
  reference: string;
}

export interface CheckResult {
  ok: boolean;
  check_id: string;
  image_name: string;
  hazards: HazardItem[];
  summary: string;
  description: string;
  error: string;
  checked_at: string;
}

export interface BatchCheckResult {
  ok: boolean;
  total: number;
  results: CheckResult[];
  summary: string;
}

export interface RiskHistoryItem {
  check_id: string;
  image_name: string;
  thumbnail: string;
  summary: string;
  description: string;
  hazard_count: number;
  has_risk: boolean | null;
  risk_level: string;
  checked_at: string;
}

export interface RiskHistoryDetail {
  check_id: string;
  image_name: string;
  thumbnail: string;
  checked_at: string;
  result: CheckResult;
}

export interface FireFacilityItem {
  category: string;
  name: string;
  specification: string;
  quantity_or_coverage: string;
  installation_location: string;
  regulation_ref: string;
  priority: 'mandatory' | 'recommended' | 'optional';
}

export interface BuildingMaterialItem {
  category: string;
  name: string;
  specification: string;
  application_location: string;
  regulation_ref: string;
  priority: 'mandatory' | 'recommended' | 'optional';
}

export interface SafetyPreparationItem {
  category: string;
  name: string;
  requirement: string;
  regulation_ref: string;
  priority: 'mandatory' | 'recommended' | 'optional';
}

export interface FireSafetyRequest {
  building_type: string;
  building_height: number;
  floor_count_above: number;
  floor_count_below: number;
  building_area: number;
  fire_resistance_rating: string;
  structural_form: string;
  fire_hazard_category: string;
  occupancy_count: number;
  has_sprinkler_system: boolean;
  has_alarm_system: boolean;
  has_hydrant_system: boolean;
  construction_status: string;
  additional_notes: string;
  room_type?: string;
  area?: number;
  height?: number;
  equipment?: string;
  special_requirements?: string;
}

export interface FireSafetyItem {
  name: string;
  quantity: string;
  specification: string;
  location: string;
  reason: string;
}

export interface FireSafetyHistoryItem {
  record_id: string;
  building_type: string;
  risk_level: string;
  summary: string;
  building_height: number;
  building_area: number;
  created_at: string;
}

export interface FireSafetyHistoryDetail {
  record_id: string;
  building_type: string;
  risk_level: string;
  created_at: string;
  request: FireSafetyRequest;
  result: FireSafetyResult;
}

export interface FireSafetyResult {
  ok: boolean;
  building_type: string;
  building_height: number;
  building_area: number;
  structural_form: string;
  risk_level: string;
  summary: string;
  fire_facilities: FireFacilityItem[];
  building_materials: BuildingMaterialItem[];
  safety_preparations: SafetyPreparationItem[];
  applicable_standards: string[];
  recommendations: FireSafetyItem[];
  room_type: string;
  notes: string;
  regulation_refs: string;
  error: string;
}

export interface ApiResponse<T = unknown> {
  ok: boolean;
  msg?: string;
  data?: T;
  detail?: string;
}

export interface PaginatedData<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface AdminUser {
  user_id: string;
  phone: string;
  name: string;
  user_type: string;
  status: string;
  roles: string[];
  permissions: string[];
  direct_permissions: string[];
  created_at: string;
}

export interface Role {
  role_id: string;
  name: string;
  description: string;
  permissions: string[];
  is_builtin: boolean;
}

export interface Application {
  application_id: string;
  user_id: string;
  phone: string;
  name: string;
  requested_roles: string[];
  requested_permissions: string[];
  reason: string;
  status: 'pending' | 'approved' | 'rejected';
  reviewer_id?: string;
  review_reason?: string;
  created_at: string;
}

export interface VideoTaskParams {
  aspect_ratio: string;
  resolution: string;
  voice: string;
  subtitle_style: string;
}

export interface VideoTask {
  task_id: string;
  original_filename: string;
  params: VideoTaskParams;
  status: string;
  progress: number;
  progress_text: string;
  result_url?: string;
  error?: string;
  created_at: string;
}

export interface AuditLog {
  log_id: string;
  user_id?: string;
  phone?: string;
  operator_name?: string;
  action: string;
  resource: string;
  detail: string;
  ip: string;
  created_at: string;
}
