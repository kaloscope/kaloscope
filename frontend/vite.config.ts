import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { withPwa } from '@vite-pwa/sveltekit';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import devtoolsJson from 'vite-plugin-devtools-json';

export default defineConfig({
  plugins: [
    devtoolsJson(),
    tailwindcss(),
    withPwa(
      {
        preprocess: vitePreprocess(),
        adapter: adapter({
          pages: 'build',
          assets: 'build',
          fallback: '404.html',
          precompress: false,
          strict: true
        }),
        paths: {
          relative: false
        },
        serviceWorker: {
          register: false
        },
        compilerOptions: {
          runes: true
        }
      },
      {
        strategies: 'buildSW',
        filename: 'service-worker.js',
        // retain precaching for the shared editor and media bundle
        maximumFileSizeToCacheInBytes: 4 * 1024 * 1024,
        scope: '/',
        base: '/',
        pwaAssets: {
          config: true
        },
        manifest: {
          short_name: 'Kaloscope',
          name: 'Kaloscope',
          start_url: '/',
          scope: '/',
          display: 'standalone',
          theme_color: '#ffffff',
          background_color: '#ffffff'
        },
        buildSW: {
          swSrc: 'src/pwa/service-worker.ts',
          globPatterns: ['client/**/*.{js,css,ico,png,svg,webp,woff,woff2}']
        },
        devOptions: {
          enabled: true,
          type: 'module',
          navigateFallback: '/'
        },
        kit: {
          includeVersionFile: true
        }
      }
    )
  ],
  resolve: {
    alias: {
      $lib: fileURLToPath(new URL('./src/lib', import.meta.url))
    }
  },
  server: {
    strictPort: true,
    host: '0.0.0.0',
    proxy: {
      '/_api': 'http://127.0.0.1:8000'
    }
  }
});
