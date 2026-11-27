import type { AnimationConfig, FlipParams } from 'svelte/animate';
import { flip as _flip } from 'svelte/animate';
import { prefersReducedMotion } from 'svelte/motion';
import type { FlyParams, ScaleParams, SlideParams, TransitionConfig } from 'svelte/transition';
import { fade, fly as _fly, scale as _scale, slide as _slide } from 'svelte/transition';

/**
 * Fade an element instead of moving it when the user prefers reduced motion.
 *
 * @param node - Transitioned element.
 * @param params - Transition parameters whose delay, duration, and easing the fade keeps.
 * @returns The fade transition configuration.
 */
function still(node: Element, { delay, duration, easing }: FlyParams | ScaleParams | SlideParams): TransitionConfig {
  return fade(node, { delay, duration, easing });
}

/**
 * Fly an element in or out, or fade it when the user prefers reduced motion.
 *
 * @param node - Transitioned element.
 * @param params - The `svelte/transition` `fly` parameters.
 * @returns The transition configuration.
 */
export function fly(node: Element, params: FlyParams = {}): TransitionConfig {
  return prefersReducedMotion.current ? still(node, params) : _fly(node, params);
}

/**
 * Scale an element in or out, or fade it when the user prefers reduced motion.
 *
 * @param node - Transitioned element.
 * @param params - The `svelte/transition` `scale` parameters.
 * @returns The transition configuration.
 */
export function scale(node: Element, params: ScaleParams = {}): TransitionConfig {
  return prefersReducedMotion.current ? still(node, params) : _scale(node, params);
}

/**
 * Slide an element in or out, or fade it when the user prefers reduced motion.
 *
 * @param node - Transitioned element.
 * @param params - The `svelte/transition` `slide` parameters.
 * @returns The transition configuration.
 */
export function slide(node: Element, params: SlideParams = {}): TransitionConfig {
  return prefersReducedMotion.current ? still(node, params) : _slide(node, params);
}

/**
 * Animate a reordered element to its new position, or move it at once when the user prefers reduced motion.
 *
 * @param node - Animated element.
 * @param rects - The element's positions before and after the reorder.
 * @param rects.from - The position before the reorder.
 * @param rects.to - The position after the reorder.
 * @param params - The `svelte/animate` `flip` parameters.
 * @returns The animation configuration.
 */
export function flip(node: Element, rects: { from: DOMRect; to: DOMRect }, params: FlipParams = {}): AnimationConfig {
  return prefersReducedMotion.current ? { duration: 0 } : _flip(node, rects, params);
}
