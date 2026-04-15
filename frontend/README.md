# Harmonograf frontend

React + TypeScript + Vite dev server for the Harmonograf console.

## Running against a server

`pnpm dev` starts Vite on `http://127.0.0.1:5173`. The app talks gRPC-Web to
`harmonograf_server` at the URL in `VITE_HARMONOGRAF_API`; if that env var is
unset, it falls back to `http://127.0.0.1:7532`, which matches the server's
default `--web-port`. So:

```bash
# Terminal 1
python -m harmonograf_server           # binds 127.0.0.1:7532 (gRPC-Web)
# Terminal 2
pnpm dev                               # Vite on :5173, talks to :7532
```

To point the frontend at a non-default server (different port or host), set
`VITE_HARMONOGRAF_API` before launching Vite:

```bash
VITE_HARMONOGRAF_API=http://127.0.0.1:17532 pnpm dev
```

The env var name is `VITE_HARMONOGRAF_API` (the `VITE_` prefix is required so
Vite exposes it to the browser bundle). If the URL is unreachable the session
picker falls back to a "Server unreachable — showing demo sessions" view.

---

## Vite template notes

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...

      // Remove tseslint.configs.recommended and replace with this
      tseslint.configs.recommendedTypeChecked,
      // Alternatively, use this for stricter rules
      tseslint.configs.strictTypeChecked,
      // Optionally, add this for stylistic rules
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```

You can also install [eslint-plugin-react-x](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x'
import reactDom from 'eslint-plugin-react-dom'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs['recommended-typescript'],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```
