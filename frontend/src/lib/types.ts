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

export interface JobOut {
  id: string
  project_id: string
  job_type: string
  status: string
  stats: { total?: number; done?: number; failed?: number; skipped?: number }
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
  data_dir: string
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
}

export interface ImportResult {
  created: number
  errors: { index: number; message: string }[]
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
