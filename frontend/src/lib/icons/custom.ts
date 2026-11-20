import type { IconifyIcon } from 'iconify-icon';

/**
 * Hand-drawn full-color icons on a 48 by 48 grid that have no Iconify source.
 *
 * Their colors are fixed, so they ignore `currentColor` and look the same wherever they appear.
 */
export const customIcons = {
  // open clapperboard with red stripes, scene lines, and a play badge
  clapperboard: {
    width: 48,
    height: 48,
    body:
      '<path fill="#465063" d="M5 21h38v18.5a3.5 3.5 0 0 1-3.5 3.5h-31A3.5 3.5 0 0 1 5 39.5z"/>' +
      '<path fill="#3a4354" d="M5 38h38v1.5a3.5 3.5 0 0 1-3.5 3.5h-31A3.5 3.5 0 0 1 5 39.5z"/>' +
      '<path fill="#f0525e" d="M5 21h38v7H5z"/>' +
      '<path fill="#fff" d="M10 21h4.5l-4 7H6zm9 0h4.5l-4 7H15zm9 0h4.5l-4 7H24zm9 0h4.5l-4 7H33z"/>' +
      '<g transform="rotate(-14 5 21)"><rect width="38" height="7" x="5" y="14" fill="#f0525e" rx="1.5"/>' +
      '<path fill="#fff" d="M7 14h4.5l4 7H11zm9 0h4.5l4 7H20zm9 0h4.5l4 7H29zm9 0h4.5l4 7H38z"/></g>' +
      '<circle cx="6.6" cy="19.6" r="1.7" fill="#ffc94a"/>' +
      '<rect width="15" height="2.6" x="10" y="31.5" fill="#97a2b6" rx="1.3"/>' +
      '<rect width="9" height="2.6" x="10" y="36" fill="#97a2b6" rx="1.3"/>' +
      '<circle cx="34.5" cy="34" r="4.8" fill="#ffc94a"/>' +
      '<path fill="#465063" stroke="#465063" stroke-linejoin="round" d="M33 31.5v5l4.2-2.5z"/>'
  },
  // retro television with antennas, knobs, and color bars on the screen
  retroTv: {
    width: 48,
    height: 48,
    body:
      '<path fill="none" stroke="#5b6474" stroke-linecap="round" stroke-width="2.4" d="M24 14 15.5 5.5M24 14l8.5-9"/>' +
      '<circle cx="15.5" cy="5.5" r="2.1" fill="#f0525e"/><circle cx="32.5" cy="5" r="2.1" fill="#ffc94a"/>' +
      '<path fill="#5b6474" d="M19 15a5 4.2 0 0 1 10 0z"/>' +
      '<path fill="none" stroke="#3b4352" stroke-linecap="round" stroke-width="2.8" d="m12 40-2 5m26-5 2 5"/>' +
      '<rect width="40" height="28" x="4" y="14" fill="#4c8df6" rx="5"/>' +
      '<path fill="#3b74dc" d="M4 37h40a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5z"/>' +
      '<rect width="26" height="20" x="8" y="18" fill="#1f2937" rx="3.5"/>' +
      '<path fill="#eef0f4" d="M10 20h3.15v16H10z"/><path fill="#ffd23f" d="M13.15 20h3.15v16h-3.15z"/>' +
      '<path fill="#3fd0e0" d="M16.3 20h3.15v16H16.3z"/><path fill="#4cd07d" d="M19.45 20h3.15v16h-3.15z"/>' +
      '<path fill="#ff5fa2" d="M22.6 20h3.15v16H22.6z"/><path fill="#f0525e" d="M25.75 20h3.15v16h-3.15z"/>' +
      '<path fill="#6b74ff" d="M28.9 20H32v16h-3.1z"/>' +
      '<circle cx="38.6" cy="22.5" r="2.3" fill="#ffd23f"/><circle cx="38.6" cy="29" r="2.3" fill="#eef0f4"/>' +
      '<path fill="none" stroke="#2f5fb8" stroke-linecap="round" stroke-width="1.3" d="M36.6 33.6h4"/>'
  },
  // open book with a brown cover, lines of text, and a ribbon bookmark
  openBook: {
    width: 48,
    height: 48,
    body:
      '<path fill="#8f5631" d="M24 16.5C18.3 13.5 11.3 12 2.5 12v28.5c8.8 0 15.8 1 21.5 4 5.7-3 12.7-4 21.5-4V12' +
      'c-8.8 0-15.8 1.5-21.5 4.5z"/>' +
      '<path fill="#f0dfbd" d="M24 14C19.4 11 13.4 9.5 6 9.5v27c7.4 0 13.4 1.2 18 4z"/>' +
      '<path fill="#fdf5e3" d="M24 14c4.6-3 10.6-4.5 18-4.5v27c-7.4 0-13.4 1.2-18 4z"/>' +
      '<path fill="none" stroke="#dcc497" stroke-width="1.3" d="M24 14v26.5"/>' +
      '<path fill="none" stroke="#c2a473" stroke-linecap="round" stroke-width="1.9" ' +
      'd="m9.5 15.8 9.5 1.6m-9.5 3.2 9.5 1.5m-9.5 3.3 9.5 1.4m-9.5 3.4 6.5.9m17-13.4 6-1.1M33 22.5l6-.9' +
      'M33 27.2l6-.8M33 31.9l6-.7"/>' +
      '<path fill="#ec4452" d="M27 12.6c1.15-.6 2.3-1.1 3.6-1.5v35.4l-1.8-2-1.8 2z"/>'
  },
  // two stacked pictures with a sky, a sun, and mountains, after `fluent:image-multiple-24-regular`
  imageStack: {
    width: 48,
    height: 48,
    body:
      '<rect width="32" height="32" x="10.5" y="10.5" fill="#8b6cf0" rx="6"/>' +
      '<path fill="#7457dc" d="M41.95 39a6 6 0 0 1-5.45 3.5h-20a6 6 0 0 1-5.45-3.5z"/>' +
      '<rect width="32" height="32" x="6" y="6" fill="#7fd4fb" rx="6"/>' +
      '<circle cx="29.5" cy="14.5" r="3.6" fill="#ffd23f"/>' +
      '<path fill="#5fd3c0" d="m17 38 11.5-14.5a1.6 1.6 0 0 1 2.5 0L38 32a6 6 0 0 1-6 6z"/>' +
      '<path fill="#1fa59a" d="M6 30.5 15.2 20a1.8 1.8 0 0 1 2.7 0L33 38H12a6 6 0 0 1-6-6z"/>'
  }
} satisfies Record<string, IconifyIcon>;
