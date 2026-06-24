import type { IconifyIcon } from 'iconify-icon';

/**
 * Fluent UI System Icons by [Microsoft Corporation](https://github.com/microsoft/fluentui-system-icons)
 *
 * {@link https://icon-sets.iconify.design/fluent/ }
 */
const fluentUISystemIcons = {
  addCircle: 'fluent:add-circle-24-regular',
  alertUrgent: 'fluent:alert-urgent-24-regular',
  appsList: 'fluent:apps-list-24-regular',
  appsListDetail: 'fluent:apps-list-detail-24-regular',
  appsListDetailFilled: 'fluent:apps-list-detail-24-filled',
  appStore: 'fluent:app-store-24-regular',
  arrowCircleDownSplit: 'fluent:arrow-circle-down-split-24-regular',
  arrowHookUpLeft: 'fluent:arrow-hook-up-left-24-regular',
  arrowHookUpRight: 'fluent:arrow-hook-up-right-24-regular',
  arrowNextFilled: 'fluent:arrow-next-24-filled',
  arrowPreviousFilled: 'fluent:arrow-previous-24-filled',
  arrowReset: 'fluent:arrow-reset-24-regular',
  arrowRotateClockwise: 'fluent:arrow-rotate-clockwise-24-regular',
  arrowRouting: 'fluent:arrow-routing-24-regular',
  arrowSortDownLines: 'fluent:arrow-sort-down-lines-24-regular',
  arrowSortUpLines: 'fluent:arrow-sort-up-lines-24-regular',
  arrowSyncCircle: 'fluent:arrow-sync-circle-24-regular',
  bookGlobe: 'fluent:book-globe-24-regular',
  bookQuestionMark: 'fluent:book-question-mark-24-regular',
  boxArrowUp: 'fluent:box-arrow-up-24-regular',
  boxMultipleSearch: 'fluent:box-multiple-search-24-regular',
  boxMultipleSearchFilled: 'fluent:box-multiple-search-24-filled',
  bracesVariable: 'fluent:braces-variable-24-regular',
  calendar: 'fluent:calendar-24-regular',
  chatWarning: 'fluent:chat-warning-24-regular',
  checkmark: 'fluent:checkmark-24-regular',
  checkmarkCircle: 'fluent:checkmark-circle-24-regular',
  clipboardCode: 'fluent:clipboard-code-24-regular',
  cloudCube: 'fluent:cloud-cube-24-regular',
  code: 'fluent:code-24-regular',
  contentView: 'fluent:content-view-24-regular',
  contentViewFilled: 'fluent:content-view-24-filled',
  delete: 'fluent:delete-24-regular',
  deleteDismiss: 'fluent:delete-dismiss-24-regular',
  desktop: 'fluent:desktop-24-regular',
  desktopArrowDown: 'fluent:desktop-arrow-down-24-regular',
  dismiss: 'fluent:dismiss-24-regular',
  dismissCircle: 'fluent:dismiss-circle-24-regular',
  dismissCircleFilled: 'fluent:dismiss-circle-24-filled',
  document: 'fluent:document-24-regular',
  documentAdd: 'fluent:document-add-24-regular',
  documentCopy: 'fluent:document-copy-24-regular',
  documentEdit: 'fluent:document-edit-24-regular',
  documentFlowchart: 'fluent:document-flowchart-24-regular',
  documentSignature: 'fluent:document-signature-24-regular',
  eye: 'fluent:eye-24-regular',
  flowchart: 'fluent:flowchart-24-regular',
  flowchartFilled: 'fluent:flowchart-24-filled',
  folder: 'fluent:folder-24-regular',
  folderSearch: 'fluent:folder-search-24-regular',
  fullScreenMaximizeFilled: 'fluent:full-screen-maximize-24-filled',
  fullScreenMinimizeFilled: 'fluent:full-screen-minimize-24-filled',
  globe: 'fluent:globe-24-regular',
  globeDesktop: 'fluent:globe-desktop-24-regular',
  globeSearch: 'fluent:globe-search-24-regular',
  globeSearchFilled: 'fluent:globe-search-24-filled',
  grid: 'fluent:grid-24-regular',
  image: 'fluent:image-24-regular',
  info: 'fluent:info-24-regular',
  key: 'fluent:key-24-regular',
  keyFilled: 'fluent:key-24-filled',
  layerDiagonalAdd: 'fluent:layer-diagonal-add-24-regular',
  layerDiagonalSparkle: 'fluent:layer-diagonal-sparkle-24-regular',
  library: 'fluent:library-24-regular',
  line: 'fluent:line-24-regular',
  lockClosed: 'fluent:lock-closed-24-regular',
  lockClosedKey: 'fluent:lock-closed-key-24-regular',
  lockOpen: 'fluent:lock-open-24-regular',
  moreVertical: 'fluent:more-vertical-24-regular',
  moviesAndTv: 'fluent:movies-and-tv-24-regular',
  pageFit: 'fluent:page-fit-24-regular',
  panelLeftText: 'fluent:panel-left-text-24-regular',
  pause: 'fluent:pause-24-regular',
  pauseFilled: 'fluent:pause-24-filled',
  peopleSettings: 'fluent:people-settings-24-regular',
  personAdd: 'fluent:person-add-24-regular',
  personEdit: 'fluent:person-edit-24-regular',
  phone: 'fluent:phone-24-regular',
  phoneTablet: 'fluent:phone-tablet-24-regular',
  pictureInPictureEnter: 'fluent:picture-in-picture-enter-24-regular',
  pictureInPictureExit: 'fluent:picture-in-picture-exit-24-regular',
  play: 'fluent:play-24-regular',
  playCircle: 'fluent:play-circle-24-regular',
  playFilled: 'fluent:play-24-filled',
  questionCircle: 'fluent:question-circle-24-regular',
  record: 'fluent:record-24-regular',
  rowChild: 'fluent:row-child-24-regular',
  save: 'fluent:save-24-regular',
  search: 'fluent:search-24-regular',
  searchInfo: 'fluent:search-info-24-regular',
  selectObjectSkewEdit: 'fluent:select-object-skew-edit-24-regular',
  serverLink: 'fluent:server-link-24-regular',
  settings: 'fluent:settings-24-regular',
  settingsFilled: 'fluent:settings-24-filled',
  signOut: 'fluent:sign-out-24-regular',
  slideSearch: 'fluent:slide-search-24-regular',
  speaker1Filled: 'fluent:speaker-1-24-filled',
  speaker2Filled: 'fluent:speaker-2-24-filled',
  speakerMuteFilled: 'fluent:speaker-mute-24-filled',
  star: 'fluent:star-24-regular',
  starFilled: 'fluent:star-24-filled',
  starHalf: 'fluent:star-half-24-regular',
  starOneQuarter: 'fluent:star-one-quarter-24-regular',
  starThreeQuarter: 'fluent:star-three-quarter-24-regular',
  subtractCircle: 'fluent:subtract-circle-24-regular',
  tablet: 'fluent:tablet-24-regular',
  textGrammarSettings: 'fluent:text-grammar-settings-24-regular',
  videoClipMultiple: 'fluent:video-clip-multiple-24-regular',
  videoClipMultipleFilled: 'fluent:video-clip-multiple-24-filled',
  warning: 'fluent:warning-24-regular',
  wrenchSettings: 'fluent:wrench-settings-24-regular'
};

/**
 * Mage Icons by [MageIcons](https://github.com/Mage-Icons/mage-icons)
 *
 * {@link https://icon-sets.iconify.design/mage/ }
 */
const mageIcons = {
  alignRight: 'mage:align-right',
  box3d: 'mage:box-3d',
  box3dDownload: 'mage:box-3d-download',
  box3dDownloadFill: 'mage:box-3d-download-fill',
  box3dScanFill: 'mage:box-3d-scan-fill',
  clock: 'mage:clock',
  dashboardBar: 'mage:dashboard-bar',
  dashboardBarFill: 'mage:dashboard-bar-fill',
  directionUpRight: 'mage:direction-up-right-2',
  download: 'mage:download',
  edit: 'mage:edit',
  externalLink: 'mage:external-link',
  fileCheck: 'mage:file-check',
  magnetUp: 'mage:magnet-up',
  pinFill: 'mage:pin-fill',
  stars: 'mage:stars-c',
  user: 'mage:user',
  userFill: 'mage:user-fill'
};

/**
 * Tabler Icons by [Paweł Kuna](https://github.com/tabler/tabler-icons)
 *
 * {@link https://icon-sets.iconify.design/tabler/ }
 */
const tablerIcons = {
  adjustmentsHorizontal: 'tabler:adjustments-horizontal',
  alignBoxCenterBottom: 'tabler:align-box-center-bottom',
  alignBoxCenterTop: 'tabler:align-box-center-top',
  arrowBigDown: 'tabler:arrow-big-down',
  arrowBigUp: 'tabler:arrow-big-up',
  arrowNarrowDown: 'tabler:arrow-narrow-down',
  arrowNarrowDownDashed: 'tabler:arrow-narrow-down-dashed',
  circleDashedPlus: 'tabler:circle-dashed-plus',
  colorSwatch: 'tabler:color-swatch',
  deviceTvOld: 'tabler:device-tv-old',
  link: 'tabler:link',
  map: 'tabler:map',
  mapOff: 'tabler:map-off',
  mist: 'tabler:mist',
  palette: 'tabler:palette',
  unlink: 'tabler:unlink'
};

/**
 * IconPark by [ByteDance](https://github.com/bytedance/IconPark)
 *
 * {@link https://icon-sets.iconify.design/icon-park-outline/ }
 * {@link https://icon-sets.iconify.design/icon-park-solid/ }
 */
const iconPark = {
  arrowDown: 'icon-park-outline:arrow-down',
  arrowUp: 'icon-park-outline:arrow-up',
  back: 'icon-park-outline:back',
  backSolid: 'icon-park-solid:back',
  clear: 'icon-park-outline:clear',
  goEnd: 'icon-park-outline:go-end',
  goStart: 'icon-park-outline:go-start',
  logout: 'icon-park-outline:logout',
  menuFoldSolid: 'icon-park-solid:menu-fold-one',
  moreApp: 'icon-park-outline:more-app',
  redo: 'icon-park-outline:redo',
  sort: 'icon-park-outline:sort-two',
  switch: 'icon-park-outline:switch'
};

/**
 * MingCute Icon by [MingCute Design](https://github.com/Richard9394/MingCute)
 *
 * {@link https://icon-sets.iconify.design/mingcute/ }
 */
const mingCuteIcon = {
  danmakuFill: 'mingcute:danmaku-fill',
  listCheck: 'mingcute:list-check-line',
  loading: 'mingcute:loading-line',
  shadow: 'mingcute:shadow-line',
  videoFill: 'mingcute:video-fill'
};

/**
 * Flowbite Icons by [Themesberg](https://github.com/themesberg/flowbite-icons)
 *
 * {@link https://icon-sets.iconify.design/flowbite/ }
 */
const flowbiteIcons = {
  language: 'flowbite:language-outline'
};

// export all icons as a single object
export const icons = {
  ...fluentUISystemIcons,
  ...mageIcons,
  ...tablerIcons,
  ...iconPark,
  ...mingCuteIcon,
  ...flowbiteIcons
};

/**
 * Convert an IconifyIcon to an SVG string.
 *
 * @param icon - The IconifyIcon to convert.
 * @param svgClass - Optional class to add to the SVG element.
 * @returns The SVG string representation of the icon.
 */
export function iconToSVG(icon: IconifyIcon, svgClass?: string): string {
  const left = icon.left ?? 0;
  const top = icon.top ?? 0;
  const width = icon.width ?? 16;
  const height = icon.height ?? 16;

  const attributes: Record<string, string> = {};
  attributes['xmlns'] = 'http://www.w3.org/2000/svg';
  attributes['class'] = svgClass ?? '';
  attributes['width'] = width.toString();
  attributes['height'] = height.toString();
  attributes['viewBox'] = [left, top, width, height].join(' ');

  let attribsHTML = '';
  for (const [key, value] of Object.entries(attributes)) {
    attribsHTML += ` ${key}="${value}"`;
  }
  return `<svg ${attribsHTML}>${icon.body}</svg>`;
}
