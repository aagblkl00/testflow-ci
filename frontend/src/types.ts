export interface User {
  id: string
  username: string
  display_name: string
  role: 'ADMIN' | 'TESTER' | 'VIEWER'
}

export interface Project {
  id: string
  name: string
  description: string
  repository_url: string
  created_at?: string
}

export interface Environment {
  id: string
  project_id: string
  name: string
  base_url: string
  browser: string
  environment_type: 'DEV' | 'TEST' | 'STAGING' | 'PROD'
  allowed_hosts: string[]
  read_only: boolean
  variables: Record<string, string>
}

export interface TestStep {
  id?: string
  sort_order: number
  action: string
  element_id: string
  value: string
  expected: string
  timeout: number
  retry: number
  screenshot: boolean
}

export interface TestCase {
  id: string
  project_id: string
  case_code: string
  name: string
  module: string
  priority: string
  status: string
  tags: string[]
  description: string
  preconditions: string
  expected: string
  created_by: string
  steps: TestStep[]
  flow_id: string
  flow_name: string
}

export interface FlowStep {
  name: string
  action: string
  locator_type: string
  locator_value: string
  value: string
  expected: string
  timeout: number
  retries: number
  screenshot: boolean
  continue_on_failure: boolean
  disabled: boolean
}

export interface AutomationFlow {
  id: string
  project_id: string
  name: string
  version: number
  engine: string
  environment_id: string
  variables: Record<string, string>
  definition: { steps?: FlowStep[] }
}

export interface SuiteMember {
  id: string
  case_id: string
  sort_order: number
  enabled: boolean
  case_code: string
  case_name: string
  module: string
  priority: string
  case_status: string
  flow_id: string
  flow_name: string
}

export interface TestSuite {
  id: string
  project_id: string
  name: string
  description: string
  enabled: boolean
  created_at?: string
  members: SuiteMember[]
  case_count: number
  enabled_case_count: number
}

export interface RegressionPlan {
  id: string
  project_id: string
  name: string
  description: string
  environment_id: string | null
  suite_id: string | null
  case_ids: string[]
  browser: string
  trigger_type: string
  schedule: string
  retry_count: number
  fail_retry: boolean
  notification_enabled: boolean
  enabled: boolean
  suite_name: string
  resolved_case_count: number
}

export interface ExecutionRun {
  id: string
  project_id: string
  plan_id: string | null
  environment_id: string
  status: string
  trigger_source: string
  commit_sha: string
  branch: string
  // M4：CI 关联信息（后端 RunOut 一直有，前端类型此前漏了）
  pipeline_id: string
  pipeline_url: string
  base_url_override: string
  total_cases: number
  passed_cases: number
  failed_cases: number
  skipped_cases: number
  error_cases: number
  flaky_cases: number
  cancelled_cases: number
  source_run_id: string | null
  rerun_mode: string
  cancel_requested: boolean
  duration_ms: number
  error_message: string
  started_at: string | null
  cancelled_at: string | null
  finished_at: string | null
  created_at: string | null
  plan_name: string
  environment_name: string
}

export interface StepResult {
  id: string
  attempt_id: string | null
  step_no: number
  step_name: string
  action: string
  status: string
  expected: string
  actual: string
  duration_ms: number
  error: string
  attempt: number
  screenshot_path: string
  page_url: string
  page_title: string
  dom_path: string
  log_path: string
}

export interface Attempt {
  id: string
  case_result_id: string
  attempt_no: number
  status: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number
  error_message: string
  trace_path: string
  video_path: string
  har_path: string
  step_results: StepResult[]
}

export interface CaseResult {
  id: string
  run_id: string
  case_id: string | null
  case_code: string
  case_name: string
  flow_id: string
  status: string
  attempts_count: number
  flaky: boolean
  duration_ms: number
  error_message: string
  attempts: Attempt[]
  step_results: StepResult[]
}

export interface Artifact {
  id: string
  run_id: string
  case_result_id: string | null
  step_result_id: string | null
  attempt_id: string | null
  type: string
  path: string
  size: number
  url: string
}

// ---------------------------------------------------------------- M3 失败中心

export interface FailureFingerprint {
  id: string
  fingerprint: string
  strategy: string
  failure_type: string
  exception_type: string
  normalized_message: string
  normalized_expected: string
  normalized_actual: string
  page_url: string
  request_method: string
  request_url: string
  http_status: number | null
  locator: string
  step_action: string
  sample_message: string
  created_at: string | null
  updated_at: string | null
  occurrence_groups: number
}

export interface RetryDecision {
  category: string
  auto_retry: boolean
  explicit_retry_count: number
  blocked: boolean
  case_is_flaky: boolean
  retries: number
  attempts: number
  source: string
}

export interface FailureGroup {
  id: string
  project_id: string
  project_name: string
  fingerprint_id: string
  title: string
  category: string
  rule_id: string
  rule_reason: string
  status: string
  first_seen_at: string | null
  last_seen_at: string | null
  occurrence_count: number
  failed_case_count: number
  failed_run_count: number
  flaky_count: number
  latest_run_id: string
  latest_case_result_id: string
  created_at: string | null
  updated_at: string | null
  fingerprint: FailureFingerprint | null
  retry_policy: Partial<RetryDecision>
}

export interface FailureGroupPage {
  items: FailureGroup[]
  total: number
  page: number
  page_size: number
  pages: number
  category_counts: Record<string, number>
}

export interface FailureOccurrence {
  id: string
  failure_group_id: string
  fingerprint_id: string
  run_id: string
  case_result_id: string
  attempt_id: string | null
  step_result_id: string | null
  case_status: string
  is_flaky: boolean
  category: string
  created_at: string | null
  case_code: string
  case_name: string
  run_status: string
  run_started_at: string | null
  // M4：CI 关联信息（沿 Run 带出）
  run_branch: string
  run_commit_sha: string
  run_pipeline_id: string
  run_pipeline_url: string
  run_trigger_source: string
  plan_id: string | null
  plan_name: string
  environment_name: string
  attempt_no: number
  attempt_status: string
  step_no: number
  step_action: string
  step_status: string
  step_error: string
  step_expected: string
  step_actual: string
}

export interface FailureOccurrencePage {
  items: FailureOccurrence[]
  total: number
  page: number
  page_size: number
  pages: number
}

export interface FailureTrendPoint {
  run_id: string
  run_started_at: string | null
  run_status: string
  occurrences: number
  cases: number
  flaky_occurrences: number
}

export interface FailureTrend {
  group_id: string
  points: FailureTrendPoint[]
  total_occurrences: number
  flaky_occurrences: number
}

export interface FailureAffectedCase {
  case_id: string | null
  case_code: string
  case_name: string
  occurrences: number
  first_seen_at: string | null
  last_seen_at: string | null
  is_flaky: boolean
}

export interface FlakyStatistics {
  total_case_executions: number
  flaky_case_executions: number
  flaky_rate: number
  denominator: string
  numerator: string
  definition: string
  distinct_flaky_cases: number
  flaky_groups: {
    group_id: string
    title: string
    category: string
    flaky_occurrences: number
    all_occurrences: number
  }[]
  by_case: {
    case_id: string | null
    case_code: string
    case_name: string
    executions: number
    flaky_count: number
    flaky_rate: number
  }[]
  by_run: { run_id: string; started_at: string | null; flaky_cases: number; total_cases: number }[]
}

export interface RetryPolicy {
  defaults: Record<string, number>
  effective: Record<string, number>
  source: Record<string, unknown>
  precedence: string[]
}

export interface FailureStats {
  total_groups: number
  open_groups: number
  categories: Record<string, number>
}

// ------------------------------------------------------ M5 AI 分析 / 告警 / 通知

export interface FailureAnalysis {
  id: string
  failure_group_id: string
  failure_occurrence_id: string
  run_id: string
  provider: string
  model: string
  status: string
  revision_no: number
  prompt_version: string
  evidence_fingerprint: string
  summary: string
  root_cause: string
  evidence: string[]
  related_commit: string
  suggested_owner: string
  confidence: number | null
  uncertainty: string
  recommendation: string
  /** AI 的建议分类：与 FailureGroup.category 分开显示，绝不互相覆盖 */
  ai_suggested_category: string
  error_message: string
  duration_ms: number
  review_status: string
  reviewed_by: string
  reviewed_at: string | null
  review_comment: string
  created_at: string | null
}

export interface AnalysisListResult {
  group_id: string
  total: number
  eligible: boolean
  eligibility_reason: string
  items: FailureAnalysis[]
}

export interface Alert {
  id: string
  project_id: string
  failure_group_id: string
  run_id: string
  latest_run_id: string
  reverify_run_id: string
  severity: string
  status: string
  title: string
  summary: string
  first_seen_at: string | null
  last_seen_at: string | null
  occurrence_count: number
  failed_case_count: number
  failed_run_count: number
  acknowledged_at: string | null
  resolved_at: string | null
  resolved_by: string
  allowed_transitions: string[]
  created_at: string | null
  updated_at: string | null
  created?: boolean
  failure_group?: {
    id: string
    title: string
    category: string
    rule_id: string
    rule_reason: string
    occurrence_count: number
  } | null
  severity_rule?: { severity: string; rule_id: string; reason: string } | null
}

export interface NotificationLog {
  id: string
  alert_id: string
  channel: string
  provider: string
  status: string
  attempt_count: number
  message_id: string
  error_message: string
  sent_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface AiMeta {
  ai_enabled: boolean
  provider: string
  model: string
  prompt_version: string
  available_providers: string[]
  auto_analyze: boolean
  required_rules: string[]
  notification_enabled: boolean
  notification_provider: string
  notification_max_attempts: number
  note: string
}
