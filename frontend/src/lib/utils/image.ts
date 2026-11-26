/**
 * Draw an image into a canvas of the given pixel size, cropped like `object-fit: cover`.
 *
 * The source is halved in steps before the final draw, so large reductions stay smooth instead of aliasing.
 *
 * @param image - Loaded image to draw.
 * @param width - Target width in device pixels.
 * @param height - Target height in device pixels.
 * @returns A canvas holding the downscaled image.
 */
export function coverThumbnail(image: HTMLImageElement, width: number, height: number): HTMLCanvasElement {
  const scale = Math.max(width / image.naturalWidth, height / image.naturalHeight);
  const cropWidth = width / scale;
  const cropHeight = height / scale;
  let source: CanvasImageSource = image;
  let sourceRect = [
    (image.naturalWidth - cropWidth) / 2,
    (image.naturalHeight - cropHeight) / 2,
    cropWidth,
    cropHeight
  ] as const;

  /**
   * Draw the current source into a new canvas of the given size.
   *
   * @param w - Canvas width in pixels.
   * @param h - Canvas height in pixels.
   * @returns The new canvas.
   */
  const draw = (w: number, h: number): HTMLCanvasElement => {
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const context = canvas.getContext('2d')!;
    context.imageSmoothingQuality = 'high';
    context.drawImage(source, ...sourceRect, 0, 0, w, h);
    return canvas;
  };

  let [, , w, h] = sourceRect;
  while (w / 2 >= width * 2) {
    w = Math.round(w / 2);
    h = Math.round(h / 2);
    source = draw(w, h);
    sourceRect = [0, 0, w, h];
  }
  return draw(width, height);
}

/**
 * Sharpen a canvas in place with a 3×3 unsharp mask.
 *
 * @param canvas - Canvas to sharpen.
 * @param amount - Strength of the mask; `0` leaves the pixels unchanged.
 * @returns The same canvas.
 * @throws {DOMException} If the canvas is tainted by a cross-origin image.
 */
export function sharpenCanvas(canvas: HTMLCanvasElement, amount: number): HTMLCanvasElement {
  const context = canvas.getContext('2d')!;
  const { width, height } = canvas;
  const input = context.getImageData(0, 0, width, height);
  const output = context.createImageData(width, height);
  const src = input.data;
  const dst = output.data;

  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const index = (y * width + x) * 4;
      for (let channel = 0; channel < 3; channel++) {
        // gaussian 3×3 blur, clamped at the edges
        let sum = 0;
        for (let dy = -1; dy <= 1; dy++) {
          const row = Math.min(height - 1, Math.max(0, y + dy)) * width;
          for (let dx = -1; dx <= 1; dx++) {
            const column = Math.min(width - 1, Math.max(0, x + dx));
            const weight = (dx === 0 ? 2 : 1) * (dy === 0 ? 2 : 1);
            sum += src[(row + column) * 4 + channel] * weight;
          }
        }
        const value = src[index + channel];
        dst[index + channel] = value + amount * (value - sum / 16);
      }
      dst[index + 3] = src[index + 3];
    }
  }
  context.putImageData(output, 0, 0);
  return canvas;
}

/**
 * Build a crisp thumbnail for an image shown much smaller than its natural size.
 *
 * The image is resampled to the device-pixel size of its box and lightly sharpened, which restores the detail that
 * a large browser downscale softens.
 *
 * @param image - Loaded image element.
 * @param width - Displayed width in CSS pixels.
 * @param height - Displayed height in CSS pixels.
 * @param amount - Sharpening strength.
 * @returns An object URL of the thumbnail, or `null` when the image is not much larger than its box or cannot be
 *   read (a cross-origin source).
 */
export async function sharpThumbnail(
  image: HTMLImageElement,
  width: number,
  height: number,
  amount = 0.6
): Promise<string | null> {
  const ratio = window.devicePixelRatio || 1;
  const targetWidth = Math.round(width * ratio);
  const targetHeight = Math.round(height * ratio);
  if (!targetWidth || !targetHeight || !image.naturalWidth || !image.naturalHeight) {
    return null;
  }
  // close to the box size the browser already renders it well
  if (Math.max(targetWidth / image.naturalWidth, targetHeight / image.naturalHeight) > 2 / 3) {
    return null;
  }

  try {
    const canvas = sharpenCanvas(coverThumbnail(image, targetWidth, targetHeight), amount);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve));
    return blob ? URL.createObjectURL(blob) : null;
  } catch {
    // cross-origin pixels cannot be read back
    return null;
  }
}
