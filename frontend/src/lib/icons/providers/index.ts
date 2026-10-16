import type { Component } from 'svelte';
import OpenAI from './OpenAI.svelte';
import Anthropic from './Anthropic.svelte';
import Gemini from './Gemini.svelte';
import XAI from './XAI.svelte';
import DeepSeek from './DeepSeek.svelte';
import OpenRouter from './OpenRouter.svelte';
import Ollama from './Ollama.svelte';
import LMStudio from './LMStudio.svelte';

/** Brand icons shared by AI provider settings. */
export const providerIcons = {
  openai: OpenAI,
  anthropic: Anthropic,
  google: Gemini,
  xai: XAI,
  deepseek: DeepSeek,
  openrouter: OpenRouter,
  ollama: Ollama,
  lmstudio: LMStudio
} satisfies Record<string, Component<{ class?: string }>>;
