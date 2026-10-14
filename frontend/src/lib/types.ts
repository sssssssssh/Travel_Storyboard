// API response types (mirror backend/app/schemas.py).

export interface ProjectStats {
  asset_count: number
  asset_succeeded: number
  asset_failed: number
  segment_count: number
  total_duration_sec: number
  total_size_bytes: number
}

export interface ProjectDetail {
  id: string
  name: string
  source_roots: string[]
  output_dir: string
  settings: Record<string, unknown>
  created_at: string
  updated_at: string
  stats: ProjectStats
  /** Where derived artifacts actually land (output_dir/<id> or data/projects/<id>). */
  cache_dir: string
}

export interface AssetListItem {
  id: string
  display_name: string
  source_path: string
  file_size_bytes: number
  duration_sec: number
  width: number
  height: number
  scan_status: string
  error_code: string
  segment_count: number
}

export interface AssetListOut {
  items: AssetListItem[]
  total: number
  offset: number
  limit: number
}

export interface SegmentListItem {
  status?: string
  note?: string
  id: string
  asset_id: string
  start_sec: number
  end_sec: number
  detected_start_sec: number
  detected_end_sec: number
  is_trimmed: boolean
  duration_sec: number
  boundary_reason: string
  user_rating: string
  story_role: string
  quality_metrics: Record<string, unknown>
  content_tags: Record<string, unknown>
  thumbnail: string | null
  asset_display_name: string
}

export interface SegmentListOut {
  items: SegmentListItem[]
  total: number
  offset: number
  limit: number
}

export interface JobFailureItem {
  kind: string
  ref: string
  message: string
}

/** How much of the library the vision model actually saw (audit P1-06). */
export interface DirectAnalysisCoverage {
  library_assets?: number
  candidate_segments?: number
  candidate_assets?: number
  segments_considered?: number
  segments_covered_by_images?: number
  assets_covered_by_images?: number
  images_sent?: number
  segments_not_covered?: number
  complete?: boolean
}

export interface DirectAnalysisStory {
  title: string
  logline: string
  segment_ids: string[]
  unknown_segment_ids: string[]
}

export interface DirectAnalysisResult {
  configured: boolean
  report?: string
  stories?: DirectAnalysisStory[]
  review_requests?: {
    asset_id: string
    segment_id: string
    start_sec: number
    end_sec: number
    priority: string
    reason: string
  }[]
  coverage?: DirectAnalysisCoverage
  problems?: string[]
  retried?: boolean
  created?: number
  message?: string
}

export interface JobOut {
  id: string
  project_id: string
  job_type: string
  status: string
  stats: { total?: number; done?: number; failed?: number; skipped?: number;
    phase?: string; current_file?: string; signature_mode?: string; bytes_read?: number;
    total_bytes?: number; processed_files?: number; total_files?: number; strict_extra_bytes?: number;
    forced_reprobe?: number; skipped_unchanged?: number; hash_passes?: number; hash_restarts?: number; needs_review?: number; source_changed?: number; files_generated?: number; bytes?: number;
    skipped_budget?: number; requested_duration_sec?: number; output_duration_sec?: number;
    cleanup?: { ok: boolean; error_code?: string; retryable?: boolean; removed_files?: number;
      removed_bytes?: number; residual_count?: number; residual_bytes?: number | null; residual_paths?: string[] } }
  failures?: { count?: number; items?: JobFailureItem[] }
  resume_token: Record<string, unknown>
  error: string
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface HealthOut {
  status: string
  version: string
  ffmpeg: string | null
  ffprobe: string | null
  ffmpeg_available: boolean
}

export interface SystemConfigOut {
  bind_host: string
  bind_port: number
  data_dir_name: string
  max_concurrency: number
  thumb_width: number
  proxy_width: number
  model_name: string
  model_configured: boolean
}

export interface ReviewPackageOut {
  id: string
  project_id: string
  version: string
  package_dir: string
  status: string
  file_count: number
  total_size_bytes: number
  created_at: string
}

export interface ReviewSegmentContext {
  id: string
  asset_id: string
  display_name: string
  start_sec: number
  end_sec: number
  detected_start_sec: number
  detected_end_sec: number
  user_rating: string
  story_role: string
  note: string
  status: string
  change_reason: string
  thumbnail: string | null
}

export interface ReviewRequestOut {
  id: string
  project_id: string
  asset_id: string
  segment_id: string
  start_sec: number
  end_sec: number
  priority: string
  reason: string
  source: string
  status: string
  proxy_ref: string
  created_at: string
  supersedes_request_id?: string
  superseded_by_request_id?: string
  disposition_at?: string | null
  validation_error?: { error_code: string; message: string } | null
  segment_context?: ReviewSegmentContext | null
  current_segments?: ReviewSegmentContext[]
  plan_shot_ids?: string[]
}

export interface ImportResult {
  created: number
  errors: { index: number; request_index: number; segment_id: string; error_code: string; message: string }[]
  duplicates?: { index: number; request_id: string; message: string }[]
}

export interface EditPlanShotOut {
  id: string
  project_id: string
  order: number
  asset_id: string
  segment_id: string
  source_in_sec: number
  source_out_sec: number
  target_duration_sec: number
  timeline_start_sec: number
  story_role: string
  transition_hint: string
  music_cue: string
  audio_hint: string
  color_hint: string
  evidence: string
  confidence: number
  review_status: string
  source_name: string
}

export interface EditPlanOut {
  schema_version: string
  project_id: string
  generated_at: string
  total_duration_sec: number
  shots: EditPlanShotOut[]
}

export interface BuildPlanResult {
  created: number
  errors: { index: number; message: string }[]
  total_duration_sec?: number
}
