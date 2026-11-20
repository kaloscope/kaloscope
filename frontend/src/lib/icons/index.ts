import { icons as preloaded } from './$icons';
import { customIcons } from './custom';

// the preloaded Iconify icons plus the hand-drawn ones
export const icons = { ...preloaded, ...customIcons };
export { icons as names, iconToSVG } from './icons';
