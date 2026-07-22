import {
  DownloadState,
  GraphCategory,
  GraphState,
  IntervalUnit,
  JobState,
  JobTrigger,
  LibType,
  TranscodeState,
  TransferMethod,
  UserRole
} from '$lib/enums';
import { icons } from '$lib/icons';
import type { Edge, Node } from '@xyflow/svelte';
import type { HandleType, Position } from '@xyflow/system';
import type { IconifyIcon } from 'iconify-icon';

/**
 * Makes every property in `T` optional and nullable.
 */
export type Optional<T> = {
  [P in keyof T]?: T[P] | null;
};

/**
 * Base response interface.
 */
export interface BaseResp {
  status: number;
  message: string;
  description?: string;
}

/**
 * Response data interface.
 */
export interface Resp<T> extends BaseResp {
  request_id: string;
  data: T;
}

/**
 * A paginated list wrapper.
 */
export interface Page<T> {
  total?: number | null;
  totalPages?: number | null;
  items: T[];
}

/**
 * Options that control scrolling behavior.
 */
export type ScrollPosition = ScrollToOptions & { panel?: boolean };

/**
 * A value accepted by a selectable option.
 */
export type OptionValue = string | number | boolean | null | undefined;

/**
 * A selectable option presented to the user.
 */
export type Option = {
  value: OptionValue;
  label: string;
  disabled?: boolean;
};

/**
 * An application navigation item.
 */
export type Navigation = {
  title: string;
  path: string;
  icon: string | IconifyIcon;
  iconFilled: string | IconifyIcon;
  mobile: boolean;
  drawerStyle?: 'menu' | 'app';
};

/**
 * A route entry displayed in a menu.
 */
export type MenuRoute = {
  title: string;
  path?: string;
  icon: string | IconifyIcon;
  iconColor?: string;
  translate?: boolean;
};

/**
 * A navigation menu.
 */
export type Menu = {
  title: string;
  routes: MenuRoute[];
};

/**
 * A signpost displayed in the interface.
 */
export type Signpost = string | { title: string; translate?: boolean };

/**
 * An authentication result for the current user.
 */
export type Token = {
  token: string;
  user: User;
};

/**
 * A user account recognized by the application.
 */
export type User = {
  id: number;
  created_at: string;
  updated_at: string;
  login_id: string;
  username: string;
  avatar: string | null;
  role: keyof typeof UserRole;
  preferences: {
    homepage: string;
    vibration: boolean;
    recent_searches: boolean;
    recent_watches: boolean;
    search_records: number;
    watch_records: number;
    landscape_mode: 'rotate' | 'web_api';
    [key: string]: string | boolean | number;
  } | null;
  user_agent: string;
  client_ip: string;
  login_at: string;
  expire_at: string;
  last_activity: string | null;
};

/**
 * A notification delivered to a user.
 */
type Notification = {
  id: number;
  title: string;
  content: string;
  created_at: string;
  seen: boolean;
};

/**
 * A file-system entry used by the path browser.
 */
export type Path = {
  name: string;
  path: string;
  is_dir: boolean;
  is_empty: boolean | null;
  is_hidden: boolean;
  expandable: boolean;
  file_type: string | null;
  open?: boolean;
  loading?: boolean;
  children?: Path[] | null;
};

/**
 * Statistics reported for a file-system path.
 */
export type PathStats = {
  name: string;
  path: string;
  is_dir: boolean;
  readable: boolean;
  writable: boolean;
  size: string;
  total?: string;
  used?: string;
  free?: string;
};

/**
 * A globally available application variable.
 */
export type GlobalVariable = {
  id: number;
  created_at: string;
  updated_at: string;
  key: string;
  value: string;
  value_length: number;
  encrypted: boolean;
};

/**
 * A URL-matching rule for DNS and proxy routing.
 */
export type URLRule = {
  id: number;
  created_at: string;
  updated_at: string;
  pattern: string;
  secure_dns: boolean;
  http_proxy: boolean;
  priority: number;
  proxy_id: number | null;
  resolver_ids: number[];
};

/**
 * A secure DNS resolver configuration.
 */
export type DNSResolver = {
  id: number;
  created_at: string;
  updated_at: string;
  name: string;
  protocol: 'tls' | 'https';
  nameserver: string;
  dnssec: boolean;
};

/**
 * An HTTP or SOCKS5 proxy configuration.
 */
export type HTTPProxy = {
  id: number;
  created_at: string;
  updated_at: string;
  name: string;
  protocol: 'http' | 'socks5';
  host: string;
  port: number;
  username: string | null;
  pw_length: number;
};

/**
 * A media library managed by the application.
 */
export type MediaLib = {
  id: number;
  lib_type: keyof typeof LibType;
  dir: string;
  name: string;
  language: string | null;
  priority: number;
  danmaku_server: string | null;
  danmaku_ttl: number;
  triggers: FlowTrigger[];
  scanning: boolean;
};

/**
 * A file or directory indexed in a media library.
 */
export type MediaItem = {
  id: number;
  // lib_id: number;
  lib?: MediaLib;
  // parent_id: number | null;
  parent?: MediaItem | null;
  dir: string;
  path: string;
  name: string;
  hash: string | null;
  size: number | null;
  visible: boolean;
  nfo_path: string | null;
  nfo_mtime: string | null;
  nfo_source: string | null;
  title: string | null;
  year: number | null;
  aired: string | null;
  season: number | null;
  episode: number | null;
  poster: string | null;
  backdrop: string | null;
  rating: number | null;
  children?: MediaItem[];
  metadata?: MediaMeta | null;
};

/**
 * An actor credited in media metadata.
 */
export type Actor = {
  name: string | null;
  role: string | null;
  thumb: string | null;
};

/**
 * Descriptive metadata associated with a media item.
 */
export type MediaMeta = {
  nfo_path: string;
  nfo_source: string | null;
  unique_id: string | null;
  title: string | null;
  originaltitle: string | null;
  tagline: string | null;
  plot: string | null;
  rating: number | null;
  year: number | null;
  aired: string | null;
  season: number | null;
  episode: number | null;
  premiered: string | null;
  country: string | null;
  mpaa: string | null;
  tags: string[] | null;
  genres: string[] | null;
  studios: string[] | null;
  directors: string[] | null;
  writers: string[] | null;
  credits: string[] | null;
  actors: Actor[] | null;
  poster: string | null;
  backdrop: string | null;
};

/**
 * An embedded chapter in a media container.
 */
export type MediaChapter = {
  id: string;
  title: string;
  start: number;
  end: number;
};

/**
 * Metadata returned by the local media probe endpoint.
 */
export type MediaProbe = {
  duration: number;
  chapters: MediaChapter[];
};

/**
 * A download service configured in the application.
 */
export type Downloader = {
  id: number;
  created_at: string;
  updated_at: string;
  preset: string | null;
  config: string;
  name: string;
  host: string | null;
  port: number | null;
  version: string | null;
  priority: number;
  status: 'up' | 'down' | 'unknown';
};

/**
 * A destination directory exposed by a downloader.
 */
export type DownloadDir = {
  path: string;
  free?: string;
};

/**
 * A task managed by a download service.
 */
export type DownloadTask = {
  id: number;
  created_at: string;
  updated_at: string;
  downloader_id: number;
  dir: string;
  name: string;
  unique_id: string | null;
  info_hash: string | null;
  info_hash_v2: string | null;
  magnet_link: string | null;
  state: keyof typeof DownloadState;
  raw_state: string | null;
  error_msg: string | null;
  up_speed: number | null;
  dl_speed: number | null;
  percentage: number | null;
  total_size: number | null;
  completed_size: number | null;
  completed_at: string | null;
  ratio: string;
  estimate: string;

  // transfer options
  transfer_lib_id: number | null;
  transfer_method: keyof typeof TransferMethod | null;
  sub_pattern: string | null;
  sub_repl: string | null;
};

/**
 * A plan for scheduling automated downloads.
 */
export type DownloadPlan = {
  id: number;
  created_at: string;
  updated_at: string;
  graph_id: number;
  graph_name: string | null;
  downloader_id: number;
  dir: string;
  keyword: string;
  filters: Record<string, any> | null; // eslint-disable-line
  interval_num: number;
  interval_start: string | null;
  interval_end: string | null;
  batch_limit: number;
  total_limit: number | null;
  total_count: number;
  last_exec: string | null;
  running: boolean;

  // transfer options
  transfer_lib_id: number | null;
  transfer_method: keyof typeof TransferMethod | null;
  sub_pattern: string | null;
  sub_repl: string | null;
};

/**
 * A repository that provides workflow templates.
 */
export type FlowRepo = {
  id: number;
  created_at: string;
  updated_at: string;
  repo_name: string;
  repo_url: string;
  repo_description: string | null;
  owner_name: string | null;
  owner_url: string | null;
  owner_avatar: string | null;
  loading?: boolean;
};

/**
 * A workflow template published by a repository.
 */
export type FlowTemplate = {
  id: number;
  created_at: string;
  updated_at: string;
  repo: FlowRepo;
  path: string;
  name: string;
  icon: string | null;
  description: string | null;
  category: keyof typeof GraphCategory;
  revision: number;
  definition: {
    nodes: Node[];
    edges: Edge[];
  };
  newest: boolean;
  graphs: FlowGraph[];
};

/**
 * A graph that defines an executable workflow.
 */
export type FlowGraph = {
  id: number;
  created_at: string;
  updated_at: string;
  tmpl_id: number | null;
  name: string;
  icon: string | null;
  description: string | null;
  category: keyof typeof GraphCategory;
  revision: number | null;
  state: keyof typeof GraphState;
  draft: {
    nodes: Node[];
    edges: Edge[];
  } | null;
  editable: boolean;
  success_rate: number | null;
  average_time: number | null;
  last_exec: string | null;
  node_types: string[];
  tmpl: FlowTemplate | null;
  newest_tmpl: FlowTemplate | null;
  only_preview: boolean;
};

/**
 * A validator registry shared by the workflow graph editor.
 */
export type FlowGraphContext = {
  validators: Set<() => boolean>;
  addValidator: (validator: () => boolean) => void;
};

/**
 * A workflow attached as a trigger.
 */
export type FlowTrigger = {
  id?: number | null;
  graph_id: number;
  graph_name: string;
  asynchronous: boolean;
};

/**
 * A log entry produced during workflow execution.
 */
export type FlowLog = {
  at: string;
  type: string;
  data: Record<string, any> | null; // eslint-disable-line
  document: any; // eslint-disable-line
};

/**
 * A scheduled or repeatable workflow job.
 */
export type FlowJob = {
  id: number;
  created_at: string;
  updated_at: string;
  graph_id: number;
  graph_name: string | null;
  bootparams: Record<string, any> | null; // eslint-disable-line
  repeatable: boolean;
  recoverable: boolean;
  state: keyof typeof JobState;
  trigger: keyof typeof JobTrigger;
  run_date: string | null;
  cron_expr: string | null;
  interval_num: number | null;
  interval_unit: keyof typeof IntervalUnit | null;
  interval_start: string | null;
  interval_end: string | null;
};

/**
 * A connection handle on a workflow node.
 */
export type Handle = {
  id: string;
  handle_type: HandleType;
  position: Position;
  maxconn: number;
  style: string | null;
  tag: string | null;
};

/**
 * A configurable field in a workflow node schema.
 */
export type Field = {
  id: string;
  field_type: string;
  span?: number | null;
  label: string | null;
  tooltip: string | null;
  required: boolean;
  default: any; // eslint-disable-line
};

/**
 * The schema that defines a workflow node type.
 */
export type NodeSchema = {
  node_type: string;
  name: string;
  icon: keyof typeof icons;
  group: string;
  order: number;
  width: string | null;
  fields: Field[];
  handles: Handle[];
};

/**
 * A chapter or episode exposed by a resource.
 */
export type Chapter = {
  id?: string | null;
  url?: string | null;
  title: string;
  volume?: string | null;
};

/** A collection of related resource chapters. */
export type ChapterGroup = {
  volume: string | null;
  chapters: Chapter[];
};

/**
 * A playable definition of a video resource.
 */
export type Definition = {
  url: string;
  definition: string | number;
};

/**
 * A timed danmaku comment.
 */
export type Danmaku = {
  id?: string | null;
  text: string;
  mode?: 'scroll' | 'top' | 'bottom' | null;
  color?: string | null;
  start?: number | null;
  duration?: number | null;
};

/**
 * An external or embedded subtitle track.
 */
export type Subtitle = {
  id: string;
  type: 'external' | 'embedded';
  label: string;
  url?: string | null;
  format?: string | null;
  language?: string | null;
};

/**
 * A supported resource display mode.
 */
export type ViewMode = 'table' | 'grid';
export type ViewModes = [ViewMode, ...ViewMode[]];

/**
 * A resource returned by an indexer workflow.
 */
export type Resource = Optional<{
  id: string;
  title: string;
  cover: string;
  ranking: number;
  rating: number;
  link: string;
  size: string;
  misc: string;
  category: string;
  uploader: string;
  uploaded_at: string;
  media_type: 'video' | 'audio' | 'image' | 'text';
  url: string;
  video_type: 'mp4' | 'flv' | 'hls' | 'dash';
  text: string | string[];
  images: string[];
  image_count: number;
  definitions: Definition[];
  chapters: Chapter[];
  danmakus: Danmaku[];
  favorite: boolean;
}>;

/**
 * A resource saved to a user's favorites.
 */
export type Favorite = {
  id: number;
  created_at: string;
  updated_at: string;
  user_id: number;
  indexer_id: number;
  rsrc_id: string;
  rsrc: Resource;
  url: string | null;
};

/**
 * A search filter configuration supplied by an indexer.
 */
export type Filter = {
  type:
    | 'text'
    | 'radio'
    | 'checkbox'
    | 'select'
    | 'calendar'
    | 'calendar-range'
    | 'date'
    | 'time'
    | 'datetime'
    | 'week'
    | 'month';
  label?: string;
  options?: Record<string, string>;
};

/**
 * Authentication metadata for an indexer.
 */
export type IndexerAuth = { name?: string | null } | null;

/**
 * Configuration that controls an indexer's behavior.
 */
export type IndexerConfig = Optional<{
  auth: Optional<{
    login: Optional<{
      mode: string;
      required: boolean;
    }>;
    cookie: Optional<{
      domain: string;
      path: string;
      name: string;
    }>;
  }>;

  search: Optional<{
    display: Optional<{
      page_size: number;
      view_modes: string[];
      cover_ratio: string;
    }>;
    keyword: Optional<{
      global: boolean;
      required: boolean;
    }>;
    filters: Record<string, Filter>;
  }>;

  board: Optional<{
    display: Optional<{
      view_modes: string[];
      cover_ratio: string;
    }>;
    calendar: Optional<{
      week: boolean;
      week_start: number;
    }>;
  }>;

  details: Optional<{
    specific: Optional<{
      media_type: string;
      video_type: string;
    }>;
  }>;
}>;

/**
 * A persisted global configuration entry.
 */
export type GlobalConfig = {
  id: number;
  created_at: string;
  updated_at: string;
  key: string;
  value: any; // eslint-disable-line
};

/**
 * A hardware acceleration backend supported by real-time transcoding.
 */
export type HWAccelType = 'qsv' | 'vaapi' | 'nvenc' | 'videotoolbox';
export type TranscodeQuality = 'low' | 'medium' | 'high';
export type TranscodeResolution = 'original' | '1080p' | '720p' | '480p';

/**
 * Options that control real-time media transcoding.
 */
export type TranscodeOptions = {
  hwaccel: HWAccelType | null;
  quality: TranscodeQuality;
  resolution: TranscodeResolution;
};

/**
 * A transcoding task displayed in system monitoring.
 */
export type TranscodeTask = {
  id: string;
  name: string;
  title?: string;
  subtitle?: string;
  path: string | null;
  hash: string;
  state: keyof typeof TranscodeState;
  progress: number | null;
  duration: number | null;
  encoded_duration: number;
  encoded_segments: number;
  encoded_size: number;
  encoded_size_text: string;
  pid: number | null;
  profile: string;
  quality: TranscodeQuality | null;
  resolution: TranscodeResolution | null;
  hwaccel: HWAccelType | null;
  started_at: string | null;
  finished_at: string | null;
  error_msg: string | null;
};

/**
 * A severity level used by system log monitoring.
 */
export type SystemLogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL';

/**
 * A compact log record delivered through the system monitor stream.
 */
export type SystemLog = {
  id: number;
  level: SystemLogLevel;
  logger: string;
  message: string;
  process_id: number;
  process_name: string;
  created: number;
};

/**
 * The service-wide state of system log monitoring.
 */
export type SystemLogState = {
  paused: boolean;
  run_id: string;
  buffer_id: number;
};
