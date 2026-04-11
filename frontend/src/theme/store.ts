import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { applyTheme, type ColorBlindMode, type ThemeBase } from './themes';

interface ThemeState {
  base: ThemeBase;
  colorBlind: ColorBlindMode;
  setBase: (base: ThemeBase) => void;
  setColorBlind: (mode: ColorBlindMode) => void;
}

export const useThemeStore = create<ThemeState>()(
  persist(
    (set) => ({
      base: 'dark',
      colorBlind: 'none',
      setBase: (base) => {
        applyTheme(base, useThemeStore.getState().colorBlind);
        set({ base });
      },
      setColorBlind: (mode) => {
        applyTheme(useThemeStore.getState().base, mode);
        set({ colorBlind: mode });
      },
    }),
    {
      name: 'harmonograf-theme',
      onRehydrateStorage: () => (state) => {
        if (state) applyTheme(state.base, state.colorBlind);
      },
    },
  ),
);

export function bootstrapTheme(): void {
  const { base, colorBlind } = useThemeStore.getState();
  applyTheme(base, colorBlind);
}
