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
    analyzed?: number; images_sent?: number; calls?: number;
    usage?: Record<string, number>;
    coverage?: CoverageOut;
    cleanup?: { ok: boolean; remediated?: boolean; had_failure?: boolean; error_code?: string; retryable?: boolean; removed_files?: number;
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

// --- M5 vision (TSB-304/305/306) -----------------------------------------

/** How much of the library a run actually looked at. Never a "total" claim. */
export interface CoverageOut {
  library_segments: number
  eligible: number
  excluded: number
  invalid: number
  considered: number
  planned?: number
  analyzed?: number
  cached_valid?: number
  not_covered: number
  images_sent: number
}

export interface VisionStatusOut {
  configured: boolean
  mode: string
  model_name: string
  prompt_version: string
  schema_version: string
  supports_images: boolean
  supports_multi_image: boolean
  supports_json_schema: boolean
  supports_native_video: boolean
  max_images: number
  max_calls_per_job: number
  allow_insecure_http: boolean
}

export interface VisionPreviewOut {
  model_name: string
  configured: boolean
  segments_to_analyze: number
  estimated_images: number
  estimated_requests: number
  coverage: CoverageOut
  sends_source_video: boolean
  strips_metadata: boolean
}

export interface VisionResultOut {
  id: string
  segment_id: string
  asset_id: string
  status: string
  model_provider: string
  model_id: string
  prompt_version: string
  schema_version: string
  source_signature: string
  visual: {
    scene_tags?: string[]
    subjects?: string[]
    visible_actions?: string[]
    lighting?: string
    composition?: string
    mood?: string[]
  }
  evidence: {
    state?: string
    observed_confidence?: number
    frame_ids?: string[]
    limitations?: string[]
    warnings?: string[]
    review?: { needs_motion_review?: boolean; reasons?: string[] }
  }
  request_usage: Record<string, number>
  is_current: boolean
  error_code: string
  created_at: string
  updated_at: string
}

export interface VisionResultsPage {
  limit: number
  offset: number
  count: number
  items: VisionResultOut[]
}

/** One shot suggested for multi-frame review. `sources` names the origin so a
 *  rule-based hint is never mistaken for a model judgement. */
export interface MotionCandidateOut {
  segment_id: string
  asset_id: string
  start_sec: number
  end_sec: number
  frame_count: number
  sources: string[]
  reasons: string[]
  temporal_coverage_insufficient: boolean
  suggested_priority: string
}

/** A timestamped multi-frame motion reading (TSB-307).
 *
 *  `method` is "multiframe": stills at known timestamps, NOT continuous video.
 *  The UI must keep that distinction visible. */
export interface MotionReviewOut {
  id: string
  segment_id: string
  asset_id: string
  method: string
  status: string
  model_id: string
  prompt_version: string
  start_sec: number
  end_sec: number
  motion_type: string
  stability_risk: string
  action_summary: string
  analysis: Record<string, unknown>
  evidence: {
    method?: string
    frames?: { time_sec: number; frame_id?: string }[]
    evidence_timestamps_sec?: number[]
    limitations?: string[]
    needs_human_review?: boolean
    audio_analyzed?: boolean
    video_analyzed?: boolean
  }
  request_usage: Record<string, number>
  is_current: boolean
  /** A person's own verdict; a model re-run never clears it. */
  human_verdict: string
  human_note: string
  human_at: string | null
  input_signature: string
  human_state: 'confirmed_current' | 'needs_reconfirmation' | 'not_reviewed'
  human_verdict_id: string | null
  human_invalid_reason: string
  human_history: Array<{
    id: string
    motion_review_id: string
    input_signature: string
    verdict: string
    note: string
    created_at: string
    is_effective: boolean
  }>
  error_code: string
  created_at: string
  updated_at: string
}
