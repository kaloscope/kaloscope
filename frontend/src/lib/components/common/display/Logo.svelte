<script lang="ts" module>
  export type LogoProps = {
    /** The size of the logo. */
    size?: string;
    /** Whether to show a random kaleidoscope instead of the logo. */
    random?: boolean;
  };

  /** Colors of the bars in `logo.svg`. */
  const COLORS = ['#5170ff', '#cb6ce6', '#ff914d', '#ffde59', '#00bf63'];
  /** Radius of the kaleidoscope view in SVG units. */
  const RADIUS = 120;

  /**
   * Pick a random logo color.
   *
   * @returns The color value.
   */
  function randomColor() {
    return COLORS[Math.floor(Math.random() * COLORS.length)];
  }

  /**
   * Create a random kaleidoscope pattern from the logo colors.
   *
   * @returns The mirror count, wedge angle and clip path, backdrop tint, and the beads tumbling inside one wedge.
   */
  function createPattern() {
    const mirrors = 6 + 2 * Math.floor(Math.random() * 3);
    const angle = 360 / mirrors;
    // widen the wedge slightly so neighboring mirrors leave no seams
    const [x1, y1, x2, y2] = [-0.5, angle + 0.5].flatMap((deg) => {
      const rad = (deg * Math.PI) / 180;
      return [RADIUS * Math.cos(rad), RADIUS * Math.sin(rad)];
    });
    const beads = Array.from({ length: 24 }, () => {
      const width = 24 + Math.random() * 64;
      const height = 14 + Math.random() * 28;
      return {
        x: -width / 2,
        y: -height / 2,
        width,
        height,
        // rounded bars like the logo, or pills
        rx: Math.random() < 0.7 ? 9 : height / 2,
        fill: randomColor(),
        'fill-opacity': 0.7 + Math.random() * 0.3,
        transform: `rotate(${Math.random() * 360}) translate(${Math.random() * RADIUS} 0) rotate(${Math.random() * 180})`
      };
    });
    const clip = `M0 0L${x1} ${y1}A${RADIUS} ${RADIUS} 0 0 1 ${x2} ${y2}Z`;
    return { mirrors, angle, clip, tint: randomColor(), beads };
  }
</script>

<script lang="ts">
  let { size = '2.5rem', random = false }: LogoProps = $props();
  // the unique clip path id of this instance
  const clipId = $props.id();
  // the logo image source
  const src = new URL('/logo.svg', import.meta.url).href;
  // the current kaleidoscope pattern
  let pattern = $state.raw(createPattern());

  /**
   * Turn the kaleidoscope to a new pattern, occasionally restoring the logo.
   */
  export function spin() {
    random = Math.random() >= 0.1;
    pattern = createPattern();
  }
</script>

{#if random}
  {#key pattern}
    <svg class="kaleidoscope" viewBox="0 0 256 256" width={size} height={size} role="img" aria-label="logo">
      <defs>
        <clipPath id={clipId}><path d={pattern.clip} /></clipPath>
      </defs>
      <g transform="translate(128 128)">
        <circle r={RADIUS} fill={pattern.tint} fill-opacity="0.15" />
        {#each { length: pattern.mirrors }, i (i)}
          <!-- odd wedges reflect their even neighbors across the shared mirror -->
          <g transform="rotate({(i + (i % 2)) * pattern.angle}) scale(1 {i % 2 ? -1 : 1})" clip-path="url(#{clipId})">
            <g class="beads">
              {#each pattern.beads as bead, j (j)}
                <rect {...bead} />
              {/each}
            </g>
          </g>
        {/each}
        <circle r="122" fill="none" stroke="gray" stroke-width="9" />
      </g>
    </svg>
  {/key}
{:else}
  <img {src} alt="logo" style:width={size} style:height={size} />
{/if}

<style>
  /* the tube turns while the beads tumble the other way between the mirrors */
  .kaleidoscope {
    animation:
      kaleidoscope-open 0.6s ease-out,
      kaleidoscope-turn 24s linear infinite;
  }

  .beads {
    animation: kaleidoscope-turn 10s linear infinite reverse;
  }

  @keyframes kaleidoscope-open {
    from {
      opacity: 0;
      rotate: -120deg;
      scale: 0.4;
    }
  }

  @keyframes kaleidoscope-turn {
    to {
      transform: rotate(360deg);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .kaleidoscope,
    .beads {
      animation: none;
    }
  }
</style>
