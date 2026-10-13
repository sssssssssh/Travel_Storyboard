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

export interface JobOut {
  id: string
  project_id: string
  job_type: string
  status: string
  stats: { total?: number; done?: number; failed?: number }
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
