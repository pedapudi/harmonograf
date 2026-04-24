// MD3 color roles for the four base themes plus color-blind variants.
// Variables are written to :root as CSS custom properties; components reference them
// via var(--md-sys-color-*) so theme switching is a single class swap on <html>.

export type ThemeBase = 'dark' | 'light' | 'amoled' | 'high-contrast';
export type ColorBlindMode = 'none' | 'deuteranopia' | 'protanopia' | 'tritanopia';

export interface ThemeTokens {
  // MD3 semantic color roles
  primary: string;
  onPrimary: string;
  primaryContainer: string;
  onPrimaryContainer: string;
  secondary: string;
  onSecondary: string;
  secondaryContainer: string;
  onSecondaryContainer: string;
  tertiary: string;
  onTertiary: string;
  tertiaryContainer: string;
  onTertiaryContainer: string;
  error: string;
  onError: string;
  errorContainer: string;
  onErrorContainer: string;
  surface: string;
  onSurface: string;
  surfaceVariant: string;
  onSurfaceVariant: string;
  surfaceContainer: string;
  surfaceContainerHigh: string;
  surfaceContainerHighest: string;
  outline: string;
  outlineVariant: string;
  inversePrimary: string;
  scrim: string;
  shadow: string;
  // Span kind colors (resolved against the theme; the renderer reads these)
  kindInvocation: string;
  kindLlmCall: string;
  kindToolCall: string;
  kindUserMessage: string;
  kindAgentMessage: string;
  kindTransfer: string;
  kindWaitForHuman: string;
  kindCustom: string;
  // Goldfive call-category colors. The renderer reads these as
  // `--hg-goldfive-*` so goldfive-translated spans (judge / refine / plan /
  // reflective) render with a category-specific hue on the goldfive lane.
  goldfiveJudgeOnTask: string;
  goldfiveJudgeWarning: string;
  goldfiveJudgeCritical: string;
  goldfiveJudgeNeutral: string;
  goldfiveRefine: string;
  goldfivePlan: string;
  goldfiveReflective: string;
}

const dark: ThemeTokens = {
  primary: '#a8c8ff',
  onPrimary: '#003062',
  primaryContainer: '#00468a',
  onPrimaryContainer: '#d6e3ff',
  secondary: '#7fdba0',
  onSecondary: '#003918',
  secondaryContainer: '#005227',
  onSecondaryContainer: '#9bf8bb',
  tertiary: '#7cd5d2',
  onTertiary: '#003736',
  tertiaryContainer: '#00504e',
  onTertiaryContainer: '#98f1ee',
  error: '#ffb4ab',
  onError: '#690005',
  errorContainer: '#93000a',
  onErrorContainer: '#ffdad6',
  surface: '#10131a',
  onSurface: '#e2e2e9',
  surfaceVariant: '#43474e',
  onSurfaceVariant: '#c3c6cf',
  surfaceContainer: '#1c1f26',
  surfaceContainerHigh: '#262931',
  surfaceContainerHighest: '#31333c',
  outline: '#8d9199',
  outlineVariant: '#43474e',
  inversePrimary: '#005db3',
  scrim: '#000000',
  shadow: '#000000',
  kindInvocation: '#43474e',
  kindLlmCall: '#a8c8ff',
  kindToolCall: '#7cd5d2',
  kindUserMessage: '#7fdba0',
  kindAgentMessage: '#9bf8bb',
  kindTransfer: '#ffd479',
  kindWaitForHuman: '#ffb4ab',
  kindCustom: '#8d9199',
  goldfiveJudgeOnTask: '#3bb273',
  goldfiveJudgeWarning: '#f59e0b',
  goldfiveJudgeCritical: '#e06070',
  goldfiveJudgeNeutral: '#8d9199',
  goldfiveRefine: '#a78bfa',
  goldfivePlan: '#4fd1c5',
  goldfiveReflective: '#8d9199',
};

const light: ThemeTokens = {
  primary: '#005db3',
  onPrimary: '#ffffff',
  primaryContainer: '#d6e3ff',
  onPrimaryContainer: '#001b3e',
  secondary: '#1f6c39',
  onSecondary: '#ffffff',
  secondaryContainer: '#9bf8bb',
  onSecondaryContainer: '#002110',
  tertiary: '#006a67',
  onTertiary: '#ffffff',
  tertiaryContainer: '#98f1ee',
  onTertiaryContainer: '#00201f',
  error: '#ba1a1a',
  onError: '#ffffff',
  errorContainer: '#ffdad6',
  onErrorContainer: '#410002',
  surface: '#fafbff',
  onSurface: '#1a1c22',
  surfaceVariant: '#dfe2eb',
  onSurfaceVariant: '#43474e',
  surfaceContainer: '#eef0f7',
  surfaceContainerHigh: '#e8eaf1',
  surfaceContainerHighest: '#e2e4eb',
  outline: '#73777f',
  outlineVariant: '#c3c6cf',
  inversePrimary: '#a8c8ff',
  scrim: '#000000',
  shadow: '#000000',
  kindInvocation: '#dfe2eb',
  kindLlmCall: '#005db3',
  kindToolCall: '#006a67',
  kindUserMessage: '#1f6c39',
  kindAgentMessage: '#3f8a55',
  kindTransfer: '#a8740c',
  kindWaitForHuman: '#ba1a1a',
  kindCustom: '#73777f',
  goldfiveJudgeOnTask: '#1f8a4b',
  goldfiveJudgeWarning: '#b86500',
  goldfiveJudgeCritical: '#b4261a',
  goldfiveJudgeNeutral: '#73777f',
  goldfiveRefine: '#7c3aed',
  goldfivePlan: '#0f8f83',
  goldfiveReflective: '#73777f',
};

const amoled: ThemeTokens = {
  ...dark,
  surface: '#000000',
  surfaceContainer: '#0a0c10',
  surfaceContainerHigh: '#13151a',
  surfaceContainerHighest: '#1c1f26',
  surfaceVariant: '#1a1d22',
  scrim: '#000000',
  shadow: '#000000',
};

const highContrast: ThemeTokens = {
  ...dark,
  primary: '#cfdfff',
  secondary: '#a8ffc6',
  tertiary: '#a4fffb',
  error: '#ffd2cc',
  onSurface: '#ffffff',
  surface: '#000000',
  surfaceContainer: '#000000',
  surfaceContainerHigh: '#0a0c10',
  surfaceContainerHighest: '#13151a',
  outline: '#ffffff',
  outlineVariant: '#cfd1d8',
  kindInvocation: '#9b9fa8',
  kindLlmCall: '#cfdfff',
  kindToolCall: '#a4fffb',
  kindUserMessage: '#a8ffc6',
  kindAgentMessage: '#cfffe0',
  kindTransfer: '#ffe28f',
  kindWaitForHuman: '#ffd2cc',
  kindCustom: '#cfd1d8',
  goldfiveJudgeOnTask: '#8affb8',
  goldfiveJudgeWarning: '#ffd28f',
  goldfiveJudgeCritical: '#ffbdb6',
  goldfiveJudgeNeutral: '#cfd1d8',
  goldfiveRefine: '#d3c3ff',
  goldfivePlan: '#9ff0e8',
  goldfiveReflective: '#cfd1d8',
};

export const themes: Record<ThemeBase, ThemeTokens> = {
  dark,
  light,
  amoled,
  'high-contrast': highContrast,
};

// Color-blind kind palettes preserve perceptual distance using palettes derived
// from Wong / Tol categorical sets. They override only the kind hues, leaving
// MD3 chrome roles intact so the rest of the UI remains unchanged.
type KindKeys =
  | 'kindInvocation'
  | 'kindLlmCall'
  | 'kindToolCall'
  | 'kindUserMessage'
  | 'kindAgentMessage'
  | 'kindTransfer'
  | 'kindWaitForHuman'
  | 'kindCustom';

const colorBlindOverrides: Record<Exclude<ColorBlindMode, 'none'>, Record<KindKeys, string>> = {
  // Wong palette (suitable for deuteranopia)
  deuteranopia: {
    kindInvocation: '#444444',
    kindLlmCall: '#0072b2', // blue
    kindToolCall: '#56b4e9', // sky
    kindUserMessage: '#009e73', // bluish green
    kindAgentMessage: '#66c2a5',
    kindTransfer: '#e69f00', // orange
    kindWaitForHuman: '#d55e00', // vermillion
    kindCustom: '#999999',
  },
  protanopia: {
    kindInvocation: '#444444',
    kindLlmCall: '#1f78b4',
    kindToolCall: '#a6cee3',
    kindUserMessage: '#33a02c',
    kindAgentMessage: '#b2df8a',
    kindTransfer: '#ff7f00',
    kindWaitForHuman: '#cab2d6',
    kindCustom: '#999999',
  },
  tritanopia: {
    kindInvocation: '#444444',
    kindLlmCall: '#cc79a7', // pink
    kindToolCall: '#56b4e9',
    kindUserMessage: '#009e73',
    kindAgentMessage: '#66c2a5',
    kindTransfer: '#e69f00',
    kindWaitForHuman: '#d55e00',
    kindCustom: '#999999',
  },
};

function camelToKebab(s: string): string {
  return s.replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
}

export function applyTheme(base: ThemeBase, colorBlind: ColorBlindMode): void {
  const tokens: ThemeTokens = { ...themes[base] };
  if (colorBlind !== 'none') {
    Object.assign(tokens, colorBlindOverrides[colorBlind]);
  }
  const root = document.documentElement;
  for (const [key, value] of Object.entries(tokens)) {
    if (key.startsWith('kind') || key.startsWith('goldfive')) {
      root.style.setProperty(`--hg-${camelToKebab(key)}`, value);
    } else {
      root.style.setProperty(`--md-sys-color-${camelToKebab(key)}`, value);
    }
  }
  root.dataset.theme = base;
  root.dataset.colorBlind = colorBlind;
}
