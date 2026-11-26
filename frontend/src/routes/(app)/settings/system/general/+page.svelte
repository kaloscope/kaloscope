<script lang="ts">
  import { api } from '$lib/api';
  import { Container, Label, Select, Setting } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import type { GlobalConfig, Page, Resp } from '$lib/types';
  import { onMount } from 'svelte';

  // hardware acceleration options supported by the backend
  const hwaccelOptions = [
    { value: null, label: 'transcode.hwaccel.none' },
    { value: 'qsv', label: 'Intel QuickSync (QSV)' },
    { value: 'vaapi', label: 'Video Acceleration API (VAAPI)' },
    { value: 'nvenc', label: 'Nvidia NVENC' },
    { value: 'videotoolbox', label: 'Apple VideoToolBox' }
  ];

  // the accelerators that open a DRM render device, the only ones the VAAPI device setting applies to
  const DRM_HWACCELS = new Set(['qsv', 'vaapi']);

  // quality options
  const qualityOptions = [
    { value: 'low', label: 'transcode.quality.low' },
    { value: 'medium', label: 'transcode.quality.medium' },
    { value: 'high', label: 'transcode.quality.high' }
  ];

  // the loading state
  const loading = createLoading();

  // the config values, initialized with defaults
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let configs: Record<string, any> = $state({
    'ffmpeg.path': '',
    'vaapi.device': '',
    'transcode.enabled': false,
    'transcode.hwaccel': null,
    'transcode.quality': 'medium'
  });

  /**
   * Persist a config value to the backend.
   *
   * @param key - Config key.
   */
  function setValue(key: string) {
    api.post('config/upsert', {
      json: { key: key, value: configs[key] }
    });
  }

  /**
   * Load all configs from the backend.
   */
  function loadAll() {
    loading.start();
    api
      .get('config/list', { searchParams: { page_num: 0 } })
      .json<Resp<Page<GlobalConfig>>>()
      .then(({ data }) => {
        for (const cfg of data.items) {
          configs[cfg.key] = cfg.value;
        }
      })
      .finally(() => loading.end());
  }

  onMount(() => {
    loadAll();
  });
</script>

<Container type="settings" loading={$loading}>
  <Setting title={$_('ffmpeg.title')}>
    <fieldset class="fieldset">
      <Label tip={$_('ffmpeg.path.tip')}>
        {$_('ffmpeg.path.title')}
      </Label>
      <input
        type="text"
        class="input w-full"
        placeholder="/usr/local/bin/ffmpeg"
        bind:value={configs['ffmpeg.path']}
        onchange={() => setValue('ffmpeg.path')}
      />
    </fieldset>
  </Setting>

  <Setting title={$_('transcode.title')}>
    <fieldset class="fieldset grid-cols-2">
      <Label class="my-2 justify-start" tipPlacement="right" tip={$_('transcode.auto.tip')}>
        {$_('transcode.auto.title')}
      </Label>
      <input
        type="checkbox"
        class="toggle self-center justify-self-end"
        bind:checked={configs['transcode.auto']}
        onchange={() => setValue('transcode.auto')}
      />
    </fieldset>
    <fieldset class="fieldset">
      <Label tip={$_('transcode.quality.tip')}>
        {$_('transcode.quality.title')}
      </Label>
      <Select
        translate
        options={qualityOptions}
        bind:value={configs['transcode.quality']}
        onchange={() => setValue('transcode.quality')}
        class="w-full"
      />
    </fieldset>
    <fieldset class="fieldset">
      <Label tip={$_('transcode.hwaccel.tip')}>
        {$_('transcode.hwaccel.title')}
      </Label>
      <Select
        translate
        options={hwaccelOptions}
        bind:value={configs['transcode.hwaccel']}
        onchange={() => setValue('transcode.hwaccel')}
        class="w-full"
      />
    </fieldset>
    {#if DRM_HWACCELS.has(configs['transcode.hwaccel'])}
      <fieldset class="fieldset">
        <Label tip={$_('transcode.vaapi_device.tip')}>
          {$_('transcode.vaapi_device.title')}
        </Label>
        <input
          type="text"
          class="input w-full"
          placeholder="/dev/dri/renderD128"
          bind:value={configs['vaapi.device']}
          onchange={() => setValue('vaapi.device')}
        />
      </fieldset>
    {/if}
  </Setting>
</Container>
