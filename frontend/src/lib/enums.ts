import { icons } from '$lib/icons';
import type { IconifyIcon } from 'iconify-icon';

/**
 * Enum type.
 */
export type Enum = {
  [key: string]: {
    label: string;
    icon: IconifyIcon | null;
    iconColor: string | null;
  };
};

/**
 * Create an enum.
 *
 * @param object - The object to convert to an enum.
 * @returns The enum.
 */
export function createEnum<T extends Enum>(object: T): T {
  return object;
}

/**
 * Convert an enum to an array of options for a select input.
 *
 * @param enumObject - The enum to convert.
 * @param enumAll - Whether to include an `all` option.
 * @returns An array of options.
 */
export function enumToOptions(enumObject: Enum, enumAll: boolean = true): { value: string; label: string }[] {
  const options = Object.entries(enumObject).map(([value, { label }]) => ({ value, label }));
  if (enumAll) {
    options.unshift({ value: '', label: 'enum.all' });
  }
  return options;
}

export const UserRole = createEnum({
  user: {
    label: 'enum.user_role.user',
    icon: icons.user,
    iconColor: null
  },
  admin: {
    label: 'enum.user_role.admin',
    icon: icons.keyFilled,
    iconColor: null
  }
});

export const LibType = createEnum({
  movie: {
    label: 'enum.lib_type.movie',
    icon: icons.moviesAndTv,
    iconColor: null
  },
  tv_show: {
    label: 'enum.lib_type.tv_show',
    icon: icons.deviceTvOld,
    iconColor: null
  }
});

export const DownloadState = createEnum({
  downloading: {
    label: 'enum.download_state.downloading',
    icon: icons.download,
    iconColor: null
  },
  paused: {
    label: 'enum.download_state.paused',
    icon: icons.pauseFilled,
    iconColor: null
  },
  completed: {
    label: 'enum.download_state.completed',
    icon: icons.fileCheck,
    iconColor: 'var(--color-success)'
  },
  error: {
    label: 'enum.download_state.error',
    icon: icons.dismissCircle,
    iconColor: 'var(--color-error)'
  }
});

export const TranscodeState = createEnum({
  running: {
    label: 'enum.transcode_state.running',
    icon: icons.loading,
    iconColor: 'var(--color-success)'
  },
  stopping: {
    label: 'enum.transcode_state.stopping',
    icon: icons.pauseFilled,
    iconColor: 'var(--color-warning)'
  },
  stopped: {
    label: 'enum.transcode_state.stopped',
    icon: icons.dismissCircle,
    iconColor: null
  },
  finished: {
    label: 'enum.transcode_state.finished',
    icon: icons.fileCheck,
    iconColor: 'var(--color-success)'
  },
  error: {
    label: 'enum.transcode_state.error',
    icon: icons.dismissCircle,
    iconColor: 'var(--color-error)'
  }
});

export const TransferMethod = createEnum({
  hardlink: {
    label: 'enum.transfer_method.hardlink',
    icon: null,
    iconColor: null
  },
  symlink: {
    label: 'enum.transfer_method.symlink',
    icon: null,
    iconColor: null
  },
  move: {
    label: 'enum.transfer_method.move',
    icon: null,
    iconColor: null
  },
  copy: {
    label: 'enum.transfer_method.copy',
    icon: null,
    iconColor: null
  }
});

export const GraphCategory = createEnum({
  indexer: {
    label: 'enum.graph_category.indexer',
    icon: icons.globeSearch,
    iconColor: null
  },
  ingest: {
    label: 'enum.graph_category.ingest',
    icon: icons.library,
    iconColor: null
  },
  schedule: {
    label: 'enum.graph_category.schedule',
    icon: icons.clock,
    iconColor: null
  }
});

export const GraphState = createEnum({
  draft: {
    label: 'enum.graph_state.draft',
    icon: icons.selectObjectSkewEdit,
    iconColor: null
  },
  modified: {
    label: 'enum.graph_state.modified',
    icon: icons.checkmarkCircle,
    iconColor: null
  },
  published: {
    label: 'enum.graph_state.published',
    icon: icons.checkmarkCircle,
    iconColor: 'var(--color-success)'
  }
});

export const JobState = createEnum({
  paused: {
    label: 'enum.job_state.paused',
    icon: icons.pauseFilled,
    iconColor: null
  },
  running: {
    label: 'enum.job_state.running',
    icon: icons.play,
    iconColor: 'var(--color-success)'
  }
});

export const JobTrigger = createEnum({
  date: {
    label: 'enum.job_trigger.date',
    icon: icons.calendar,
    iconColor: null
  },
  interval: {
    label: 'enum.job_trigger.interval',
    icon: icons.arrowRotateClockwise,
    iconColor: null
  },
  cron: {
    label: 'enum.job_trigger.cron',
    icon: icons.clock,
    iconColor: null
  }
});

export const IntervalUnit = createEnum({
  seconds: {
    label: 'duration.seconds',
    icon: null,
    iconColor: null
  },
  minutes: {
    label: 'duration.minutes',
    icon: null,
    iconColor: null
  },
  hours: {
    label: 'duration.hours',
    icon: null,
    iconColor: null
  },
  days: {
    label: 'duration.days',
    icon: null,
    iconColor: null
  },
  weeks: {
    label: 'duration.weeks',
    icon: null,
    iconColor: null
  }
});
